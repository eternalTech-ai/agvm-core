# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from fastapi import APIRouter, HTTPException, Request

from brain_registry import (
    BrainRegistryError,
    brain_registry_path,
    brain_root_path,
    create_local_brain,
    load_local_brain_registry,
    refresh_local_brain_registry,
    set_active_brain,
)
from projection import (
    color_from_brainhex,
    compute_radius_value,
    heuristic_projection,
    latent_vector_to_angles,
    position_to_bucket,
    position_to_topology_brainhex,
    quantize_to_brainhex,
    scores_to_latent_vector,
)
from runtime_scope import use_runtime_brain
from sqlite_store import fetch_graph_snapshot, replace_runtime_graph


BRAIN_SYNC_APPLY_RESTORE_PATH = "/memory/brains/sync/apply-restore"
BRAIN_SYNC_RESTORE_STATUS_PATH = "/memory/brains/sync/restore-status"
BRAIN_SYNC_ROLLBACK_RESTORE_PATH = "/memory/brains/sync/rollback-restore"
BRAIN_SYNC_APPLY_RESTORE_REQUEST_SCHEMA_VERSION = (
    "agvm.core.brain_sync.apply_restore_request.v1"
)
BRAIN_SYNC_ROLLBACK_RESTORE_REQUEST_SCHEMA_VERSION = (
    "agvm.core.brain_sync.rollback_restore_request.v1"
)
BRAIN_SYNC_BUNDLE_SCHEMA_VERSION = "agvm.detwin.brain_sync.bundle.v2"
BRAIN_SYNC_LOCAL_APPLICATION_RECEIPT_SCHEMA_VERSION = (
    "agvm.core.brain_sync.local_application_receipt.v1"
)
BRAIN_SYNC_RESTORE_STATUS_SCHEMA_VERSION = "agvm.core.brain_sync.restore_status.v1"
BRAIN_SYNC_ROLLBACK_RECEIPT_SCHEMA_VERSION = (
    "agvm.core.brain_sync.rollback_receipt.v1"
)
BRAIN_SYNC_RESTORE_ERROR_SCHEMA_VERSION = "agvm.core.brain_sync.error.v1"
BRAIN_SYNC_RESTORE_TRANSACTION_SCHEMA_VERSION = (
    "agvm.core.brain_sync.restore_transaction.v1"
)
DEFAULT_MAX_RESTORE_BYTES = 512 * 1024 * 1024
MAX_RESTORE_NODES = 10_000_000
MAX_RESTORE_EDGES = 50_000_000

_BRAIN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RESTORE_TRANSACTION_ID_PATTERN = re.compile(r"^restore_tx_[0-9a-f]{64}$")
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_RESTORE_LOCK = threading.RLock()
_FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "jwt_secret",
        "openai_api_key",
        "password",
        "private_key",
        "provider_secret",
        "refresh_token",
        "secret",
        "token",
    }
)
_FORBIDDEN_SECRET_KEY_FORMS = _FORBIDDEN_SECRET_KEYS | frozenset(
    key.replace("_", "") for key in _FORBIDDEN_SECRET_KEYS
)


def create_brain_sync_restore_router() -> APIRouter:
    router = APIRouter(tags=["agvm-core-brain-sync"])

    @router.post(BRAIN_SYNC_APPLY_RESTORE_PATH)
    async def apply_restore_bundle(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=400,
                detail="brain_sync_restore_request_json_invalid",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400,
                detail="brain_sync_restore_request_object_required",
            )
        return apply_validated_restore_bundle(payload)

    @router.get(BRAIN_SYNC_RESTORE_STATUS_PATH)
    async def restore_status(request: Request) -> dict[str, Any]:
        transaction_values = request.query_params.getlist("transaction_id")
        idempotency_values = request.query_params.getlist("idempotency_key")
        unknown_parameters = set(request.query_params) - {
            "transaction_id",
            "idempotency_key",
        }
        if (
            unknown_parameters
            or len(transaction_values) > 1
            or len(idempotency_values) > 1
            or bool(transaction_values) == bool(idempotency_values)
        ):
            _raise_restore_contract_error(
                status_code=422,
                operation="status",
                code="brain_sync_restore_status_locator_invalid",
            )
        return read_restore_status(
            transaction_id=transaction_values[0] if transaction_values else None,
            idempotency_key=idempotency_values[0] if idempotency_values else None,
        )

    @router.post(BRAIN_SYNC_ROLLBACK_RESTORE_PATH)
    async def rollback_restore(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _raise_restore_contract_error(
                status_code=400,
                operation="rollback",
                code="brain_sync_restore_rollback_request_json_invalid",
                cause=exc,
            )
        if not isinstance(payload, dict):
            _raise_restore_contract_error(
                status_code=400,
                operation="rollback",
                code="brain_sync_restore_rollback_request_object_required",
            )
        return rollback_applied_restore(payload)

    return router


def read_restore_status(
    *,
    transaction_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    resolved_transaction_id = _resolve_restore_transaction_id(
        transaction_id=transaction_id,
        idempotency_key=idempotency_key,
        operation="status",
    )
    root = brain_root_path().resolve()
    transaction_path = _restore_transaction_path(root, resolved_transaction_id)
    if not root.is_dir() or not transaction_path.is_file():
        _raise_restore_contract_error(
            status_code=404,
            operation="status",
            code="brain_sync_restore_transaction_not_found",
            transaction_id=resolved_transaction_id,
        )
    with _exclusive_restore_lock(root):
        transaction = _read_restore_transaction(
            transaction_path,
            transaction_id=resolved_transaction_id,
            operation="status",
        )
        return _build_public_restore_status(transaction, root=root)


def rollback_applied_restore(request_payload: dict[str, Any]) -> dict[str, Any]:
    if (
        request_payload.get("schema_version")
        != BRAIN_SYNC_ROLLBACK_RESTORE_REQUEST_SCHEMA_VERSION
    ):
        _raise_restore_contract_error(
            status_code=422,
            operation="rollback",
            code="brain_sync_restore_rollback_request_schema_invalid",
        )
    allowed_fields = {
        "schema_version",
        "transaction_id",
        "idempotency_key",
        "expected_destination_sha256",
    }
    if set(request_payload) - allowed_fields:
        _raise_restore_contract_error(
            status_code=422,
            operation="rollback",
            code="brain_sync_restore_rollback_request_fields_invalid",
        )
    transaction_id = _validated_restore_transaction_id(
        request_payload.get("transaction_id"),
        operation="rollback",
    )
    rollback_idempotency_key = _validated_public_idempotency_key(
        request_payload.get("idempotency_key"),
        operation="rollback",
    )
    expected_destination_sha256 = str(
        request_payload.get("expected_destination_sha256") or ""
    ).strip()
    if not _SHA256_PATTERN.fullmatch(expected_destination_sha256):
        _raise_restore_contract_error(
            status_code=422,
            operation="rollback",
            code="brain_sync_restore_expected_destination_sha256_invalid",
            transaction_id=transaction_id,
        )

    root = brain_root_path().resolve()
    transaction_path = _restore_transaction_path(root, transaction_id)
    if not root.is_dir() or not transaction_path.is_file():
        _raise_restore_contract_error(
            status_code=404,
            operation="rollback",
            code="brain_sync_restore_transaction_not_found",
            transaction_id=transaction_id,
        )
    with _exclusive_restore_lock(root):
        transaction = _read_restore_transaction(
            transaction_path,
            transaction_id=transaction_id,
            operation="rollback",
        )
        return _rollback_applied_restore_under_lock(
            root=root,
            transaction_path=transaction_path,
            transaction=transaction,
            transaction_id=transaction_id,
            rollback_idempotency_key=rollback_idempotency_key,
            expected_destination_sha256=expected_destination_sha256,
        )


def apply_validated_restore_bundle(request_payload: dict[str, Any]) -> dict[str, Any]:
    if (
        request_payload.get("schema_version")
        != BRAIN_SYNC_APPLY_RESTORE_REQUEST_SCHEMA_VERSION
    ):
        raise HTTPException(
            status_code=422,
            detail="brain_sync_restore_request_schema_invalid",
        )
    if request_payload.get("overwrite_existing_confirmed") is not True:
        raise HTTPException(
            status_code=409,
            detail="brain_sync_restore_overwrite_confirmation_required",
        )
    select_after_restore = request_payload.get("select_after_restore", False)
    if not isinstance(select_after_restore, bool):
        raise HTTPException(
            status_code=422,
            detail="brain_sync_restore_select_after_restore_invalid",
        )
    bundle = _required_object(request_payload.get("bundle"), "bundle")
    validation = _validate_restore_bundle(bundle)
    requested_idempotency_key = _required_text(
        request_payload.get("idempotency_key"),
        "idempotency_key",
    )
    if not hmac.compare_digest(
        requested_idempotency_key,
        validation["idempotency_key"],
    ):
        raise HTTPException(
            status_code=409,
            detail="brain_sync_restore_idempotency_key_mismatch",
        )

    root = brain_root_path().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with _exclusive_restore_lock(root):
        return _apply_restore_under_lock(
            root=root,
            bundle=bundle,
            validation=validation,
            select_after_restore=select_after_restore,
        )


def _apply_restore_under_lock(
    *,
    root: Path,
    bundle: dict[str, Any],
    validation: dict[str, Any],
    select_after_restore: bool,
) -> dict[str, Any]:
    destination = dict(validation["destination"])
    destination_brain_id = str(destination["brain_id"])
    idempotency_key = str(validation["idempotency_key"])
    transaction_id = _restore_transaction_id(idempotency_key)
    key_digest = transaction_id.removeprefix("restore_tx_")
    receipt_id = _restore_receipt_id(
        idempotency_key=idempotency_key,
        bundle_sha256=str(validation["bundle_sha256"]),
        brain_id=destination_brain_id,
    )
    state_root = root / ".brain_sync_restore"
    transaction_path = state_root / "transactions" / f"{key_digest}.json"
    idempotency_path = state_root / "idempotency" / f"{key_digest}.json"
    receipt_path = state_root / "receipts" / f"{receipt_id}.json"
    stage_path = state_root / "staging" / f"{receipt_id}-{os.urandom(8).hex()}"
    backup_path = state_root / "backups" / destination_brain_id / receipt_id
    destination_path = root / destination_brain_id
    for candidate in (
        transaction_path,
        idempotency_path,
        receipt_path,
        stage_path,
        backup_path,
        destination_path,
    ):
        _require_within_root(candidate, root)

    fingerprint_body = {
        "idempotency_key": idempotency_key,
        "bundle_sha256": validation["bundle_sha256"],
        "content_sha256": validation["content_sha256"],
        "brain_id": destination_brain_id,
        "select_after_restore": select_after_restore,
    }
    request_fingerprint = _sha256(_canonical_json_bytes(fingerprint_body))
    existing_idempotency = _read_json_object(idempotency_path)
    if existing_idempotency:
        if not hmac.compare_digest(
            str(existing_idempotency.get("request_fingerprint") or ""),
            request_fingerprint,
        ):
            raise HTTPException(
                status_code=409,
                detail="brain_sync_restore_idempotency_replay_mismatch",
            )
        applied_transaction = _read_json_object(transaction_path)
        if str(applied_transaction.get("status") or "") == "rolled_back":
            raise HTTPException(
                status_code=409,
                detail="brain_sync_restore_transaction_rolled_back",
            )
        if str(applied_transaction.get("status") or "") in {
            "rollback_pending",
            "rollback_recovery_required",
        }:
            raise HTTPException(
                status_code=500,
                detail="brain_sync_restore_recovery_required",
            )
        receipt = _read_json_object(receipt_path)
        if not receipt:
            if (
                str(applied_transaction.get("status") or "") == "applied"
                and hmac.compare_digest(
                    str(applied_transaction.get("request_fingerprint") or ""),
                    request_fingerprint,
                )
                and isinstance(applied_transaction.get("receipt"), dict)
            ):
                receipt = dict(applied_transaction["receipt"])
                _atomic_json_write(receipt_path, receipt)
            else:
                raise HTTPException(
                    status_code=500,
                    detail="brain_sync_restore_idempotency_receipt_missing",
                )
        return receipt

    previous_transaction = _read_json_object(transaction_path)
    if previous_transaction:
        if not hmac.compare_digest(
            str(previous_transaction.get("request_fingerprint") or ""),
            request_fingerprint,
        ):
            raise HTTPException(
                status_code=409,
                detail="brain_sync_restore_idempotency_replay_mismatch",
            )
        if str(previous_transaction.get("status") or "") == "applied":
            receipt = dict(previous_transaction.get("receipt") or {})
            if not receipt:
                raise HTTPException(
                    status_code=500,
                    detail="brain_sync_restore_transaction_receipt_missing",
                )
            _atomic_json_write(receipt_path, receipt)
            _atomic_json_write(
                idempotency_path,
                {
                    "schema_version": "agvm.core.brain_sync.restore_idempotency.v1",
                    "idempotency_key": idempotency_key,
                    "request_fingerprint": request_fingerprint,
                    "receipt_id": receipt_id,
                },
            )
            return receipt
        _rollback_restore_transaction(previous_transaction, root=root)

    registry = load_local_brain_registry(brain_root=root)
    original_registry = json.loads(json.dumps(registry, ensure_ascii=False))
    existing_record = next(
        (
            dict(item)
            for item in list(registry.get("brains") or [])
            if isinstance(item, dict)
            and str(item.get("brain_id") or "") == destination_brain_id
        ),
        None,
    )
    if existing_record:
        if str(existing_record.get("storage_layout") or "") != "registry_managed":
            raise HTTPException(
                status_code=409,
                detail="brain_sync_restore_registry_managed_destination_required",
            )
        registered_path = Path(
            str(existing_record.get("registry_brain_path") or "")
        ).expanduser().resolve()
        if registered_path != destination_path.resolve():
            raise HTTPException(
                status_code=409,
                detail="brain_sync_restore_destination_path_mismatch",
            )
    elif destination_path.exists():
        raise HTTPException(
            status_code=409,
            detail="brain_sync_restore_unregistered_destination_exists",
        )

    if stage_path.exists():
        _safe_remove_tree(stage_path, required_parent=state_root / "staging")
    if backup_path.exists():
        raise HTTPException(
            status_code=409,
            detail="brain_sync_restore_backup_already_exists",
        )

    receipt = _build_restore_receipt(
        validation=validation,
        receipt_id=receipt_id,
        created=existing_record is None,
        overwritten=existing_record is not None,
        selected=bool(select_after_restore or (existing_record or {}).get("is_active")),
    )
    transaction = {
        "schema_version": BRAIN_SYNC_RESTORE_TRANSACTION_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "status": "staging",
        "request_fingerprint": request_fingerprint,
        "idempotency_key": idempotency_key,
        "destination_path": str(destination_path),
        "stage_path": str(stage_path),
        "backup_path": str(backup_path),
        "destination_existed": existing_record is not None,
        "original_registry": original_registry,
        "receipt": receipt,
    }
    _atomic_json_write(transaction_path, transaction)

    try:
        _materialize_staged_brain(
            stage_path=stage_path,
            bundle=bundle,
            validation=validation,
        )
        transaction["status"] = "staged"
        _atomic_json_write(transaction_path, transaction)

        if existing_record:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination_path, backup_path)
            transaction["backup_sha256"] = _directory_sha256(backup_path)
            transaction["status"] = "backup_created"
            _atomic_json_write(transaction_path, transaction)
        os.replace(stage_path, destination_path)
        transaction["applied_destination_sha256"] = _directory_sha256(
            destination_path
        )
        transaction["status"] = "destination_installed"
        _atomic_json_write(transaction_path, transaction)

        selected = _update_registry_after_restore(
            root=root,
            destination_brain_id=destination_brain_id,
            display_name=str(destination.get("display_name") or destination_brain_id),
            existing_record=existing_record,
            select_after_restore=select_after_restore,
        )
        if selected != bool(receipt["selected"]):
            raise RuntimeError("brain_sync_restore_selection_state_mismatch")
        transaction["applied_registry"] = load_local_brain_registry(brain_root=root)
        transaction["applied_registry_sha256"] = _sha256(
            _canonical_json_bytes(transaction["applied_registry"])
        )
        transaction["status"] = "registry_committed"
        _atomic_json_write(transaction_path, transaction)

        _atomic_json_write(receipt_path, receipt)
        _atomic_json_write(
            idempotency_path,
            {
                "schema_version": "agvm.core.brain_sync.restore_idempotency.v1",
                "idempotency_key": idempotency_key,
                "request_fingerprint": request_fingerprint,
                "receipt_id": receipt_id,
            },
        )
        transaction["apply_succeeded"] = True
        transaction["applied_at"] = receipt["applied_at"]
        transaction["status"] = "applied"
        _atomic_json_write(transaction_path, transaction)
        return receipt
    except HTTPException:
        _rollback_restore_transaction(transaction, root=root)
        raise
    except Exception as exc:
        try:
            _rollback_restore_transaction(transaction, root=root)
        except Exception as rollback_exc:
            raise HTTPException(
                status_code=500,
                detail="brain_sync_restore_recovery_required",
            ) from rollback_exc
        raise HTTPException(
            status_code=500,
            detail="brain_sync_restore_apply_failed_rolled_back",
        ) from exc


def _rollback_applied_restore_under_lock(
    *,
    root: Path,
    transaction_path: Path,
    transaction: dict[str, Any],
    transaction_id: str,
    rollback_idempotency_key: str,
    expected_destination_sha256: str,
) -> dict[str, Any]:
    state_root = root / ".brain_sync_restore"
    rollback_key_digest = hashlib.sha256(
        rollback_idempotency_key.encode("utf-8")
    ).hexdigest()
    rollback_idempotency_path = (
        state_root / "rollback_idempotency" / f"{rollback_key_digest}.json"
    )
    rollback_receipt_id = _rollback_receipt_id(
        transaction_id=transaction_id,
        idempotency_key=rollback_idempotency_key,
    )
    rollback_receipt_path = (
        state_root / "rollback_receipts" / f"{rollback_receipt_id}.json"
    )
    rollback_stage_path = (
        state_root
        / "rollback_staging"
        / f"{transaction_id}-{rollback_receipt_id}"
    )
    for candidate in (
        transaction_path,
        rollback_idempotency_path,
        rollback_receipt_path,
        rollback_stage_path,
    ):
        _require_within_root(candidate, root)

    request_fingerprint = _sha256(
        _canonical_json_bytes(
            {
                "schema_version": BRAIN_SYNC_ROLLBACK_RESTORE_REQUEST_SCHEMA_VERSION,
                "transaction_id": transaction_id,
                "expected_destination_sha256": expected_destination_sha256,
            }
        )
    )
    existing_idempotency = _read_json_object(rollback_idempotency_path)
    if existing_idempotency:
        if not hmac.compare_digest(
            str(existing_idempotency.get("request_fingerprint") or ""),
            request_fingerprint,
        ):
            _raise_restore_contract_error(
                status_code=409,
                operation="rollback",
                code="brain_sync_restore_rollback_idempotency_replay_mismatch",
                transaction_id=transaction_id,
            )
        receipt = _read_json_object(rollback_receipt_path)
        if not receipt and isinstance(transaction.get("rollback_receipt"), dict):
            receipt = dict(transaction["rollback_receipt"])
            _atomic_json_write(rollback_receipt_path, receipt)
        if not receipt or str(transaction.get("status") or "") != "rolled_back":
            _raise_restore_contract_error(
                status_code=500,
                operation="rollback",
                code="brain_sync_restore_rollback_receipt_missing",
                transaction_id=transaction_id,
                retryable=True,
            )
        return receipt

    transaction_status = str(transaction.get("status") or "")
    if transaction_status == "rolled_back":
        _raise_restore_contract_error(
            status_code=409,
            operation="rollback",
            code="brain_sync_restore_transaction_already_rolled_back",
            transaction_id=transaction_id,
        )
    if transaction_status == "rollback_pending":
        _raise_restore_contract_error(
            status_code=500,
            operation="rollback",
            code="brain_sync_restore_rollback_recovery_required",
            transaction_id=transaction_id,
            retryable=True,
        )
    if transaction_status != "applied":
        _raise_restore_contract_error(
            status_code=409,
            operation="rollback",
            code="brain_sync_restore_transaction_not_applied",
            transaction_id=transaction_id,
        )

    applied_destination_sha256 = str(
        transaction.get("applied_destination_sha256") or ""
    )
    if not _SHA256_PATTERN.fullmatch(applied_destination_sha256):
        _raise_restore_contract_error(
            status_code=409,
            operation="rollback",
            code="brain_sync_restore_destination_integrity_unavailable",
            transaction_id=transaction_id,
        )
    if not hmac.compare_digest(
        expected_destination_sha256,
        applied_destination_sha256,
    ):
        _raise_restore_contract_error(
            status_code=409,
            operation="rollback",
            code="brain_sync_restore_expected_destination_mismatch",
            transaction_id=transaction_id,
        )
    if transaction.get("destination_existed") is not True:
        _raise_restore_contract_error(
            status_code=409,
            operation="rollback",
            code="brain_sync_restore_retained_backup_required",
            transaction_id=transaction_id,
        )

    destination_path = Path(str(transaction.get("destination_path") or "")).resolve()
    backup_path = Path(str(transaction.get("backup_path") or "")).resolve()
    for candidate in (destination_path, backup_path):
        _require_within_root(candidate, root)
    apply_receipt = (
        dict(transaction.get("receipt") or {})
        if isinstance(transaction.get("receipt"), dict)
        else {}
    )
    destination_brain_id = str(apply_receipt.get("brain_id") or "")
    apply_receipt_id = str(apply_receipt.get("receipt_id") or "")
    expected_destination_path = (root / destination_brain_id).resolve()
    expected_backup_path = (
        state_root / "backups" / destination_brain_id / apply_receipt_id
    ).resolve()
    if (
        not _BRAIN_ID_PATTERN.fullmatch(destination_brain_id)
        or not apply_receipt_id
        or destination_path != expected_destination_path
        or backup_path != expected_backup_path
    ):
        _raise_restore_contract_error(
            status_code=500,
            operation="rollback",
            code="brain_sync_restore_transaction_paths_invalid",
            transaction_id=transaction_id,
        )
    if not destination_path.is_dir():
        _raise_restore_contract_error(
            status_code=409,
            operation="rollback",
            code="brain_sync_restore_destination_changed",
            transaction_id=transaction_id,
        )
    try:
        current_destination_sha256 = _directory_sha256(destination_path)
    except (OSError, RuntimeError):
        current_destination_sha256 = ""
    if not hmac.compare_digest(
        current_destination_sha256,
        applied_destination_sha256,
    ):
        _raise_restore_contract_error(
            status_code=409,
            operation="rollback",
            code="brain_sync_restore_destination_changed",
            transaction_id=transaction_id,
        )
    if not backup_path.is_dir():
        _raise_restore_contract_error(
            status_code=409,
            operation="rollback",
            code="brain_sync_restore_retained_backup_missing",
            transaction_id=transaction_id,
        )

    applied_registry = transaction.get("applied_registry")
    applied_registry_sha256 = str(transaction.get("applied_registry_sha256") or "")
    original_registry = transaction.get("original_registry")
    if (
        not isinstance(applied_registry, dict)
        or not isinstance(original_registry, dict)
        or not _SHA256_PATTERN.fullmatch(applied_registry_sha256)
    ):
        _raise_restore_contract_error(
            status_code=409,
            operation="rollback",
            code="brain_sync_restore_registry_integrity_unavailable",
            transaction_id=transaction_id,
        )
    current_registry = load_local_brain_registry(brain_root=root)
    if not hmac.compare_digest(
        _sha256(_canonical_json_bytes(current_registry)),
        applied_registry_sha256,
    ):
        _raise_restore_contract_error(
            status_code=409,
            operation="rollback",
            code="brain_sync_restore_registry_changed",
            transaction_id=transaction_id,
        )

    backup_sha256 = str(transaction.get("backup_sha256") or "")
    if not _SHA256_PATTERN.fullmatch(backup_sha256):
        try:
            backup_sha256 = _directory_sha256(backup_path)
        except (OSError, RuntimeError) as exc:
            _raise_restore_contract_error(
                status_code=409,
                operation="rollback",
                code="brain_sync_restore_retained_backup_invalid",
                transaction_id=transaction_id,
                cause=exc,
            )
    elif not hmac.compare_digest(_directory_sha256(backup_path), backup_sha256):
        _raise_restore_contract_error(
            status_code=409,
            operation="rollback",
            code="brain_sync_restore_retained_backup_changed",
            transaction_id=transaction_id,
        )
    if rollback_stage_path.exists():
        _raise_restore_contract_error(
            status_code=500,
            operation="rollback",
            code="brain_sync_restore_rollback_recovery_required",
            transaction_id=transaction_id,
            retryable=True,
        )

    rollback_receipt = _build_rollback_receipt(
        transaction=transaction,
        transaction_id=transaction_id,
        receipt_id=rollback_receipt_id,
        restored_destination_sha256=backup_sha256,
    )
    transaction["rollback"] = {
        "request_fingerprint": request_fingerprint,
        "idempotency_key_sha256": f"sha256:{rollback_key_digest}",
        "receipt_id": rollback_receipt_id,
        "stage_path": str(rollback_stage_path),
        "phase": "prepared",
    }
    transaction["status"] = "rollback_pending"
    _atomic_json_write(transaction_path, transaction)

    try:
        rollback_stage_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(destination_path, rollback_stage_path)
        transaction["rollback"]["phase"] = "destination_retained"
        _atomic_json_write(transaction_path, transaction)

        os.replace(backup_path, destination_path)
        transaction["rollback"]["phase"] = "backup_installed"
        _atomic_json_write(transaction_path, transaction)
        if not hmac.compare_digest(
            _directory_sha256(destination_path),
            backup_sha256,
        ):
            raise RuntimeError("brain_sync_restore_rollback_destination_mismatch")

        _atomic_json_write(brain_registry_path(root), original_registry)
        transaction["rollback"]["phase"] = "registry_restored"
        _atomic_json_write(transaction_path, transaction)

        _atomic_json_write(rollback_receipt_path, rollback_receipt)
        _atomic_json_write(
            rollback_idempotency_path,
            {
                "schema_version": "agvm.core.brain_sync.rollback_idempotency.v1",
                "request_fingerprint": request_fingerprint,
                "transaction_id": transaction_id,
                "receipt_id": rollback_receipt_id,
            },
        )
        transaction["rollback_receipt"] = rollback_receipt
        transaction["rolled_back_at"] = rollback_receipt["rolled_back_at"]
        transaction["status"] = "rolled_back"
        _atomic_json_write(transaction_path, transaction)
    except Exception as exc:
        try:
            _revert_public_rollback(
                root=root,
                transaction=transaction,
                transaction_path=transaction_path,
                destination_path=destination_path,
                backup_path=backup_path,
                rollback_stage_path=rollback_stage_path,
            )
            rollback_idempotency_path.unlink(missing_ok=True)
            rollback_receipt_path.unlink(missing_ok=True)
        except Exception as recovery_exc:
            transaction["status"] = "rollback_recovery_required"
            try:
                _atomic_json_write(transaction_path, transaction)
            except Exception:
                pass
            _raise_restore_contract_error(
                status_code=500,
                operation="rollback",
                code="brain_sync_restore_rollback_recovery_required",
                transaction_id=transaction_id,
                retryable=True,
                cause=recovery_exc,
            )
        _raise_restore_contract_error(
            status_code=500,
            operation="rollback",
            code="brain_sync_restore_rollback_failed_reverted",
            transaction_id=transaction_id,
            retryable=True,
            cause=exc,
        )

    try:
        _safe_remove_tree(rollback_stage_path, required_parent=state_root / "rollback_staging")
    except (OSError, RuntimeError):
        transaction["rollback_cleanup_pending"] = True
        _atomic_json_write(transaction_path, transaction)
    return rollback_receipt


def _revert_public_rollback(
    *,
    root: Path,
    transaction: dict[str, Any],
    transaction_path: Path,
    destination_path: Path,
    backup_path: Path,
    rollback_stage_path: Path,
) -> None:
    if rollback_stage_path.exists():
        if destination_path.exists():
            if backup_path.exists():
                raise RuntimeError("brain_sync_restore_rollback_revert_backup_collision")
            os.replace(destination_path, backup_path)
        os.replace(rollback_stage_path, destination_path)
    applied_registry = transaction.get("applied_registry")
    if isinstance(applied_registry, dict):
        _atomic_json_write(brain_registry_path(root), applied_registry)
    transaction["status"] = "applied"
    transaction["last_rollback_error"] = "brain_sync_restore_rollback_failed_reverted"
    transaction.pop("rollback", None)
    _atomic_json_write(transaction_path, transaction)


def _materialize_staged_brain(
    *,
    stage_path: Path,
    bundle: dict[str, Any],
    validation: dict[str, Any],
) -> None:
    storage_path = stage_path / "storage"
    storage_path.mkdir(parents=True, exist_ok=False)
    for dirname in ("documents", "source_packages", "maintenance", "mcp_logs"):
        (stage_path / dirname).mkdir(parents=True, exist_ok=False)
    snapshot = dict(validation["snapshot"])
    graph = {
        "schema_version": "agvm.graph.v1",
        "version": str(validation["revision"]),
        "graph_name": str(validation["destination"]["brain_id"]),
        "brain_id": str(validation["destination"]["brain_id"]),
        "nodes": [
            _normalized_restore_node(item, index)
            for index, item in enumerate(snapshot["nodes"])
        ],
        "edges": [_normalized_restore_edge(item) for item in snapshot["edges"]],
        "meta": {
            "source": "detwin_cloud_restore",
            "transfer_direction": "cloud_to_local",
            "bundle_sha256": validation["bundle_sha256"],
            "content_sha256": validation["content_sha256"],
            "revision": validation["revision"],
            "node_count": validation["node_count"],
            "edge_count": validation["edge_count"],
        },
    }
    staged_record = {
        "brain_id": str(validation["destination"]["brain_id"]),
        "storage_path": str(storage_path),
    }
    with use_runtime_brain(staged_record):
        replace_runtime_graph(graph)
        materialized_graph = fetch_graph_snapshot()
    _atomic_json_write(storage_path / "brain_sync_sources.json", snapshot["sources"])
    _atomic_json_write(storage_path / "brain_sync_profile.json", snapshot["profile"])
    _atomic_json_write(storage_path / "brain_sync_revisions.json", snapshot["revisions"])
    _atomic_json_write(
        storage_path / "brain_sync_restore_state.json",
        {
            "schema_version": "agvm.core.brain_sync.restore_state.v1",
            "source_brain_id": validation["source"]["brain_id"],
            "brain_id": validation["destination"]["brain_id"],
            "idempotency_key": validation["idempotency_key"],
            "bundle_sha256": validation["bundle_sha256"],
            "content_sha256": validation["content_sha256"],
            "revision": validation["revision"],
            "node_count": validation["node_count"],
            "edge_count": validation["edge_count"],
        },
    )
    if (
        len(list(materialized_graph.get("nodes") or [])) != validation["node_count"]
        or len(list(materialized_graph.get("edges") or [])) != validation["edge_count"]
    ):
        raise HTTPException(
            status_code=500,
            detail="brain_sync_restore_materialized_count_mismatch",
        )
    required_files = (
        "beta_vector_memory.sqlite3",
        "beta_vector_memory.graph.json",
        "beta_vector_memory.index.json",
        "beta_vector_memory.atlas.json",
        "brain_sync_sources.json",
        "brain_sync_profile.json",
        "brain_sync_revisions.json",
        "brain_sync_restore_state.json",
    )
    if any(not (storage_path / filename).is_file() for filename in required_files):
        raise HTTPException(
            status_code=500,
            detail="brain_sync_restore_materialized_artifacts_incomplete",
        )


def _update_registry_after_restore(
    *,
    root: Path,
    destination_brain_id: str,
    display_name: str,
    existing_record: dict[str, Any] | None,
    select_after_restore: bool,
) -> bool:
    try:
        if existing_record is None:
            create_local_brain(
                brain_root=root,
                brain_id=destination_brain_id,
                display_name=display_name,
                description="Local brain restored from an explicit Detwin Cloud restore bundle.",
                make_active=False,
                make_default=False,
            )
        registry = refresh_local_brain_registry(brain_root=root)
        should_select = bool(select_after_restore or (existing_record or {}).get("is_active"))
        if should_select:
            registry = set_active_brain(
                destination_brain_id,
                make_default=bool((existing_record or {}).get("is_default")),
                brain_root=root,
            )
        record = next(
            (
                dict(item)
                for item in list(registry.get("brains") or [])
                if isinstance(item, dict)
                and str(item.get("brain_id") or "") == destination_brain_id
            ),
            None,
        )
    except BrainRegistryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not record or record.get("safe_for_mcp") is not True:
        raise HTTPException(
            status_code=500,
            detail="brain_sync_restore_destination_not_selectable",
        )
    return bool(record.get("is_active"))


def _rollback_restore_transaction(transaction: dict[str, Any], *, root: Path) -> None:
    destination_path = Path(str(transaction.get("destination_path") or "")).resolve()
    stage_path = Path(str(transaction.get("stage_path") or "")).resolve()
    backup_path = Path(str(transaction.get("backup_path") or "")).resolve()
    for path in (destination_path, stage_path, backup_path):
        _require_within_root(path, root)
    destination_existed = bool(transaction.get("destination_existed"))
    status = str(transaction.get("status") or "")
    if destination_existed and backup_path.exists():
        if destination_path.exists():
            _safe_remove_tree(destination_path, required_parent=root)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup_path, destination_path)
    elif not destination_existed and destination_path.exists():
        _safe_remove_tree(destination_path, required_parent=root)
    elif destination_existed and status in {"backup_created", "destination_installed", "registry_committed"}:
        raise RuntimeError("brain_sync_restore_rollback_backup_missing")
    if stage_path.exists():
        _safe_remove_tree(stage_path, required_parent=root / ".brain_sync_restore" / "staging")
    original_registry = transaction.get("original_registry")
    if isinstance(original_registry, dict):
        _atomic_json_write(brain_registry_path(root), original_registry)
    transaction["status"] = "apply_rolled_back"
    transaction_path = (
        root
        / ".brain_sync_restore"
        / "transactions"
        / f"{hashlib.sha256(str(transaction.get('idempotency_key') or '').encode('utf-8')).hexdigest()}.json"
    )
    _atomic_json_write(transaction_path, transaction)


def _build_public_restore_status(
    transaction: dict[str, Any],
    *,
    root: Path,
) -> dict[str, Any]:
    transaction_id = str(transaction["transaction_id"])
    internal_status = str(transaction.get("status") or "")
    if internal_status == "applied":
        public_status = "applied"
    elif internal_status == "rolled_back":
        public_status = "rolled_back"
    elif internal_status in {
        "apply_rolled_back",
        "rollback_recovery_required",
    }:
        public_status = "failed"
    else:
        public_status = "in_progress"

    expected_destination_sha256 = str(
        transaction.get("applied_destination_sha256") or ""
    )
    destination_unchanged: bool | None = None
    registry_unchanged: bool | None = None
    if internal_status == "applied":
        destination_path = Path(
            str(transaction.get("destination_path") or "")
        ).resolve()
        try:
            _require_within_root(destination_path, root)
            destination_unchanged = bool(
                _SHA256_PATTERN.fullmatch(expected_destination_sha256)
                and destination_path.is_dir()
                and hmac.compare_digest(
                    _directory_sha256(destination_path),
                    expected_destination_sha256,
                )
            )
        except (HTTPException, OSError, RuntimeError):
            destination_unchanged = False
        expected_registry_sha256 = str(
            transaction.get("applied_registry_sha256") or ""
        )
        try:
            registry_unchanged = bool(
                _SHA256_PATTERN.fullmatch(expected_registry_sha256)
                and hmac.compare_digest(
                    _sha256(
                        _canonical_json_bytes(
                            load_local_brain_registry(brain_root=root)
                        )
                    ),
                    expected_registry_sha256,
                )
            )
        except (HTTPException, OSError, ValueError):
            registry_unchanged = False

    backup_path = Path(str(transaction.get("backup_path") or "")).resolve()
    try:
        _require_within_root(backup_path, root)
        backup_retained = backup_path.is_dir()
    except HTTPException:
        backup_retained = False
    receipt = (
        dict(transaction.get("receipt") or {})
        if isinstance(transaction.get("receipt"), dict)
        else {}
    )
    rollback_receipt = (
        dict(transaction.get("rollback_receipt") or {})
        if isinstance(transaction.get("rollback_receipt"), dict)
        else {}
    )
    return {
        "schema_version": BRAIN_SYNC_RESTORE_STATUS_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "status": public_status,
        "receipt_id": str(receipt.get("receipt_id") or "") or None,
        "rollback_receipt_id": (
            str(rollback_receipt.get("receipt_id") or "") or None
        ),
        "brain_id": str(receipt.get("brain_id") or "") or None,
        "revision": receipt.get("revision"),
        "node_count": receipt.get("node_count"),
        "edge_count": receipt.get("edge_count"),
        "destination_sha256": (
            expected_destination_sha256
            if _SHA256_PATTERN.fullmatch(expected_destination_sha256)
            else None
        ),
        "destination_unchanged": destination_unchanged,
        "backup_retained": backup_retained,
        "rollback_available": bool(
            internal_status == "applied"
            and transaction.get("destination_existed") is True
            and destination_unchanged is True
            and registry_unchanged is True
            and backup_retained
        ),
        "applied_at": str(transaction.get("applied_at") or "") or None,
        "rolled_back_at": str(transaction.get("rolled_back_at") or "") or None,
    }


def _validate_restore_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    if bundle.get("schema_version") != BRAIN_SYNC_BUNDLE_SCHEMA_VERSION:
        raise HTTPException(status_code=422, detail="brain_sync_restore_bundle_schema_invalid")
    if bundle.get("bundle_type") != "restore_snapshot":
        raise HTTPException(status_code=422, detail="brain_sync_restore_bundle_type_invalid")
    if bundle.get("transfer_direction") != "cloud_to_local":
        raise HTTPException(status_code=422, detail="brain_sync_restore_direction_invalid")
    tenant = _required_object(bundle.get("tenant"), "bundle_tenant")
    source = _required_object(bundle.get("source"), "bundle_source")
    destination = _required_object(bundle.get("destination"), "bundle_destination")
    tenant = {
        "organization_id": _required_text(tenant.get("organization_id"), "organization_id"),
        "workspace_id": _required_text(tenant.get("workspace_id"), "workspace_id"),
    }
    source_brain_id = _required_text(source.get("brain_id"), "source_brain_id")
    if len(source_brain_id) > 256:
        raise HTTPException(status_code=422, detail="brain_sync_restore_source_brain_id_invalid")
    source = {**source, "brain_id": source_brain_id}
    destination_brain_id = _required_text(destination.get("brain_id"), "brain_id")
    if not _BRAIN_ID_PATTERN.fullmatch(destination_brain_id):
        raise HTTPException(status_code=422, detail="brain_sync_restore_brain_id_invalid")
    idempotency_key = _required_text(bundle.get("idempotency_key"), "bundle_idempotency_key")
    if len(idempotency_key) > 512 or "\n" in idempotency_key or "\r" in idempotency_key:
        raise HTTPException(status_code=422, detail="brain_sync_restore_idempotency_key_invalid")
    revision = _required_object(bundle.get("revision"), "bundle_revision")
    expected_revision = _non_negative_int(
        revision.get("expected_destination_revision"),
        "expected_destination_revision",
    )
    target_revision = _non_negative_int(revision.get("target_revision"), "target_revision")
    source_revision = _non_negative_int(revision.get("source_revision"), "source_revision")
    if target_revision != expected_revision + 1 or source_revision <= expected_revision:
        raise HTTPException(status_code=409, detail="brain_sync_restore_revision_invalid")
    consent = _required_object(bundle.get("consent"), "bundle_consent")
    if consent.get("sync_to_cloud") is not True:
        raise HTTPException(status_code=403, detail="brain_sync_restore_consent_required")
    payload = _required_object(bundle.get("payload"), "bundle_payload")
    if _contains_forbidden_secret(payload):
        raise HTTPException(status_code=422, detail="brain_sync_restore_bundle_contains_secret")
    snapshot = _required_object(payload.get("snapshot", payload), "snapshot")
    nodes = snapshot.get("nodes")
    edges = snapshot.get("edges")
    sources = snapshot.get("sources", [])
    profile = snapshot.get("profile", {})
    revisions = snapshot.get("revisions", {})
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise HTTPException(status_code=422, detail="brain_sync_restore_snapshot_arrays_required")
    if not isinstance(sources, list) or not isinstance(profile, dict) or not isinstance(revisions, (dict, list)):
        raise HTTPException(status_code=422, detail="brain_sync_restore_snapshot_sidecars_invalid")
    _validate_snapshot_references(nodes=nodes, edges=edges, sources=sources)
    counts = _required_object(bundle.get("counts"), "bundle_counts")
    revision_history = revisions.get("history", []) if isinstance(revisions, dict) else revisions
    actual_counts = {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "source_count": len(sources),
        "revision_count": len(revision_history) if isinstance(revision_history, list) else 0,
        "byte_count": len(_canonical_json_bytes(payload)),
    }
    for name, actual in actual_counts.items():
        if _non_negative_int(counts.get(name), name) != actual:
            raise HTTPException(status_code=409, detail=f"brain_sync_restore_{name}_mismatch")
    if actual_counts["node_count"] > MAX_RESTORE_NODES or actual_counts["edge_count"] > MAX_RESTORE_EDGES:
        raise HTTPException(status_code=413, detail="brain_sync_restore_count_limit_exceeded")
    max_bytes = _restore_max_bytes()
    if actual_counts["byte_count"] > max_bytes:
        raise HTTPException(status_code=413, detail="brain_sync_restore_size_limit_exceeded")

    bundle_body = dict(bundle)
    bundle_body.pop("checksum", None)
    bundle_body.pop("signature", None)
    checksum = _sha256(_canonical_json_bytes(bundle_body))
    signature = _required_object(bundle.get("signature"), "bundle_signature")
    if (
        not hmac.compare_digest(str(bundle.get("checksum") or ""), checksum)
        or signature.get("alg") != "sha256-canonical-json"
        or not hmac.compare_digest(str(signature.get("value") or ""), checksum)
        or signature.get("covered_fields") != "all fields except checksum/signature"
    ):
        raise HTTPException(status_code=409, detail="brain_sync_restore_bundle_checksum_invalid")
    return {
        "tenant": tenant,
        "source": source,
        "destination": destination,
        "idempotency_key": idempotency_key,
        "bundle_sha256": _sha256(_canonical_json_bytes(bundle)),
        "content_sha256": _sha256(_canonical_json_bytes(payload)),
        "revision": target_revision,
        "node_count": actual_counts["node_count"],
        "edge_count": actual_counts["edge_count"],
        "snapshot": {
            "nodes": nodes,
            "edges": edges,
            "sources": sources,
            "profile": profile,
            "revisions": revisions,
        },
    }


def _validate_snapshot_references(
    *,
    nodes: list[Any],
    edges: list[Any],
    sources: list[Any],
) -> None:
    source_ids: set[str] = set()
    for value in sources:
        source = _required_object(value, "snapshot_source")
        source_id = _required_text(source.get("id") or source.get("source_id"), "source_id")
        if source_id in source_ids:
            raise HTTPException(status_code=422, detail="brain_sync_restore_duplicate_source_id")
        source_ids.add(source_id)
    node_ids: set[str] = set()
    for value in nodes:
        node = _required_object(value, "node")
        node_id = _required_text(node.get("id"), "node_id")
        if node_id in node_ids:
            raise HTTPException(status_code=422, detail="brain_sync_restore_duplicate_node_id")
        node_ids.add(node_id)
        source_id = str(node.get("source_id") or "").strip()
        if source_id and source_ids and source_id not in source_ids:
            raise HTTPException(status_code=422, detail="brain_sync_restore_node_source_missing")
    edge_ids: set[str] = set()
    for value in edges:
        edge = _required_object(value, "edge")
        source_id = _required_text(edge.get("source") or edge.get("source_node_id"), "edge_source")
        target_id = _required_text(edge.get("target") or edge.get("target_node_id"), "edge_target")
        if source_id not in node_ids or target_id not in node_ids:
            raise HTTPException(status_code=422, detail="brain_sync_restore_dangling_edge")
        edge_id = str(edge.get("id") or "").strip()
        if edge_id and edge_id in edge_ids:
            raise HTTPException(status_code=422, detail="brain_sync_restore_duplicate_edge_id")
        edge_ids.add(edge_id)


def _normalized_restore_node(value: Any, index: int) -> dict[str, Any]:
    node = _required_object(value, "node")
    node_id = _required_text(node.get("id"), "node_id")
    raw_text = str(node.get("raw_text") or node.get("text") or node.get("summary") or node.get("label") or node_id)
    projection = heuristic_projection(
        raw_text,
        input_mode="document" if str(node.get("kind") or "").lower() in {"document", "source"} else "auto",
        node_kind_hint=str(node.get("node_kind") or node.get("kind") or "fact"),
    )
    position = node.get("final_position") or node.get("base_position") or node.get("position")
    if not isinstance(position, dict) or not {"x", "y", "z"}.issubset(position):
        angle = float(index) * 2.399963229728653
        layer = (float(index % 17) / 16.0) * 2.0 - 1.0
        radial = max(0.12, (1.0 - layer * layer) ** 0.5)
        position = {
            "x": radial * math.cos(angle),
            "y": layer * 0.82,
            "z": radial * math.sin(angle) * 0.72,
        }
    clean_position = {axis: float(position[axis]) for axis in ("x", "y", "z")}
    routing_scores = dict(projection["routing_semantic_scores"])
    routing_facets = dict(projection["routing_facets"])
    latent = scores_to_latent_vector(routing_scores)
    angles = latent_vector_to_angles(latent)
    routing_brainhex = quantize_to_brainhex(
        angles["theta"],
        angles["phi"],
        compute_radius_value(
            routing_scores,
            routing_facets,
            is_summary=bool(projection.get("is_summary")),
            granularity=float(projection.get("granularity") or 0.5),
            novelty=float(projection.get("novelty") or 0.5),
        ),
    )
    topology_brainhex = position_to_topology_brainhex(clean_position)
    provenance = node.get("provenance") if isinstance(node.get("provenance"), dict) else {}
    return {
        **node,
        "id": node_id,
        "raw_text": raw_text,
        "summary": str(node.get("summary") or raw_text),
        "node_kind": str(node.get("node_kind") or projection["node_kind"] or "memory"),
        "memory_type": str(node.get("memory_type") or projection["memory_type"] or "fact"),
        "base_position": clean_position,
        "final_position": clean_position,
        "routing_semantic_scores": routing_scores,
        "routing_facets": routing_facets,
        "routing_brainhex": routing_brainhex,
        "semantic_color": color_from_brainhex(routing_brainhex),
        "topology_brainhex": topology_brainhex,
        "topology_color": color_from_brainhex(topology_brainhex),
        "bucket": position_to_bucket(clean_position),
        "is_document_anchor": bool(node.get("is_document_anchor", False)),
        "is_summary": bool(node.get("is_summary", projection.get("is_summary"))),
        "granularity": float(node.get("granularity", projection.get("granularity") or 0.5)),
        "novelty": float(node.get("novelty", projection.get("novelty") or 0.5)),
        "links": list(node.get("links") or []),
        "highways": list(node.get("highways") or []),
        "provenance": {
            **provenance,
            "mode": str(provenance.get("mode") or "brain_sync"),
            "source_label": str(provenance.get("source_label") or "Detwin Brain Sync"),
            "source_type": str(provenance.get("source_type") or "detwin_brain_sync"),
        },
        "source_trust": str(node.get("source_trust") or "user_asserted"),
        "claim_status": str(node.get("claim_status") or "fact"),
        "answer_eligible": bool(node.get("answer_eligible", True)),
        "profile_eligible": bool(node.get("profile_eligible", True)),
        "document_eligible": bool(node.get("document_eligible", True)),
        "lifecycle_status": str(node.get("lifecycle_status") or "active"),
    }


def _normalized_restore_edge(value: Any) -> dict[str, Any]:
    edge = _required_object(value, "edge")
    source_node_id = _required_text(edge.get("source_node_id") or edge.get("source"), "edge_source")
    target_node_id = _required_text(edge.get("target_node_id") or edge.get("target"), "edge_target")
    original_kind = str(edge.get("edge_type") or edge.get("kind") or "related")
    edge_type = original_kind if original_kind in {"derives_from", "mentions_entity"} else "derives_from"
    reason = str(edge.get("reason") or "brain_sync_restore")
    if original_kind != edge_type:
        reason = f"{reason}; imported_edge_type={original_kind}"
    return {
        **edge,
        "source_node_id": source_node_id,
        "target_node_id": target_node_id,
        "edge_type": edge_type,
        "confidence": float(edge.get("confidence") or 1.0),
        "reason": reason,
    }


def _build_restore_receipt(
    *,
    validation: dict[str, Any],
    receipt_id: str,
    created: bool,
    overwritten: bool,
    selected: bool,
) -> dict[str, Any]:
    body = {
        "schema_version": BRAIN_SYNC_LOCAL_APPLICATION_RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "receipt_type": "cloud_restore_imported",
        "status": "applied",
        "applied": True,
        "transfer_direction": "cloud_to_local",
        "tenant": validation["tenant"],
        "source_brain_id": validation["source"]["brain_id"],
        "brain_id": validation["destination"]["brain_id"],
        "content_sha256": validation["content_sha256"],
        "bundle_sha256": validation["bundle_sha256"],
        "node_count": validation["node_count"],
        "edge_count": validation["edge_count"],
        "revision": validation["revision"],
        "overwrite_existing_confirmed": True,
        "created": created,
        "overwritten": overwritten,
        "selected": selected,
        "applied_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    return {**body, "receipt_sha256": _sha256(_canonical_json_bytes(body))}


def _build_rollback_receipt(
    *,
    transaction: dict[str, Any],
    transaction_id: str,
    receipt_id: str,
    restored_destination_sha256: str,
) -> dict[str, Any]:
    apply_receipt = dict(transaction.get("receipt") or {})
    body = {
        "schema_version": BRAIN_SYNC_ROLLBACK_RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "receipt_type": "local_restore_rolled_back",
        "transaction_id": transaction_id,
        "status": "rolled_back",
        "rolled_back": True,
        "brain_id": str(apply_receipt.get("brain_id") or ""),
        "apply_receipt_id": str(apply_receipt.get("receipt_id") or ""),
        "replaced_destination_sha256": str(
            transaction.get("applied_destination_sha256") or ""
        ),
        "restored_destination_sha256": restored_destination_sha256,
        "rolled_back_at": _utc_timestamp(),
    }
    return {**body, "receipt_sha256": _sha256(_canonical_json_bytes(body))}


def _restore_receipt_id(*, idempotency_key: str, bundle_sha256: str, brain_id: str) -> str:
    seed = f"{idempotency_key}:{bundle_sha256}:{brain_id}"
    return f"localapply_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


def _rollback_receipt_id(*, transaction_id: str, idempotency_key: str) -> str:
    seed = f"{transaction_id}:{idempotency_key}"
    return f"localrollback_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


def _restore_transaction_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"restore_tx_{digest}"


def _validated_restore_transaction_id(value: Any, *, operation: str) -> str:
    transaction_id = str(value or "").strip()
    if not _RESTORE_TRANSACTION_ID_PATTERN.fullmatch(transaction_id):
        _raise_restore_contract_error(
            status_code=422,
            operation=operation,
            code="brain_sync_restore_transaction_id_invalid",
        )
    return transaction_id


def _validated_public_idempotency_key(value: Any, *, operation: str) -> str:
    idempotency_key = str(value or "").strip()
    if (
        not idempotency_key
        or len(idempotency_key) > 512
        or "\n" in idempotency_key
        or "\r" in idempotency_key
    ):
        _raise_restore_contract_error(
            status_code=422,
            operation=operation,
            code="brain_sync_restore_idempotency_key_invalid",
        )
    return idempotency_key


def _resolve_restore_transaction_id(
    *,
    transaction_id: str | None,
    idempotency_key: str | None,
    operation: str,
) -> str:
    if bool(transaction_id) == bool(idempotency_key):
        _raise_restore_contract_error(
            status_code=422,
            operation=operation,
            code="brain_sync_restore_status_locator_invalid",
        )
    if transaction_id is not None:
        return _validated_restore_transaction_id(transaction_id, operation=operation)
    return _restore_transaction_id(
        _validated_public_idempotency_key(idempotency_key, operation=operation)
    )


def _restore_transaction_path(root: Path, transaction_id: str) -> Path:
    key_digest = transaction_id.removeprefix("restore_tx_")
    path = root / ".brain_sync_restore" / "transactions" / f"{key_digest}.json"
    _require_within_root(path, root)
    return path


def _read_restore_transaction(
    transaction_path: Path,
    *,
    transaction_id: str,
    operation: str,
) -> dict[str, Any]:
    transaction = _read_json_object(transaction_path)
    if not transaction:
        _raise_restore_contract_error(
            status_code=500,
            operation=operation,
            code="brain_sync_restore_transaction_unreadable",
            transaction_id=transaction_id,
            retryable=True,
        )
    if (
        transaction.get("schema_version")
        != BRAIN_SYNC_RESTORE_TRANSACTION_SCHEMA_VERSION
    ):
        _raise_restore_contract_error(
            status_code=500,
            operation=operation,
            code="brain_sync_restore_transaction_schema_invalid",
            transaction_id=transaction_id,
        )
    persisted_transaction_id = str(transaction.get("transaction_id") or "")
    if persisted_transaction_id and not hmac.compare_digest(
        persisted_transaction_id,
        transaction_id,
    ):
        _raise_restore_contract_error(
            status_code=500,
            operation=operation,
            code="brain_sync_restore_transaction_identity_mismatch",
            transaction_id=transaction_id,
        )
    persisted_idempotency_key = str(transaction.get("idempotency_key") or "")
    if (
        not persisted_idempotency_key
        or not hmac.compare_digest(
            _restore_transaction_id(persisted_idempotency_key),
            transaction_id,
        )
    ):
        _raise_restore_contract_error(
            status_code=500,
            operation=operation,
            code="brain_sync_restore_transaction_identity_mismatch",
            transaction_id=transaction_id,
        )
    transaction["transaction_id"] = transaction_id
    return transaction


def _raise_restore_contract_error(
    *,
    status_code: int,
    operation: str,
    code: str,
    transaction_id: str | None = None,
    retryable: bool = False,
    cause: BaseException | None = None,
) -> None:
    error = HTTPException(
        status_code=status_code,
        detail={
            "schema_version": BRAIN_SYNC_RESTORE_ERROR_SCHEMA_VERSION,
            "operation": operation,
            "code": code,
            "retryable": retryable,
            "http_status": status_code,
            "transaction_id": transaction_id,
        },
    )
    if cause is not None:
        raise error from cause
    raise error


def _utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _required_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HTTPException(status_code=422, detail=f"brain_sync_restore_{field}_object_required")
    try:
        return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"brain_sync_restore_{field}_json_invalid") from exc


def _required_text(value: Any, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise HTTPException(status_code=422, detail=f"brain_sync_restore_{field}_required")
    return clean


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HTTPException(status_code=422, detail=f"brain_sync_restore_{field}_invalid")
    return value


def _contains_forbidden_secret(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")
            if (
                normalized in _FORBIDDEN_SECRET_KEY_FORMS
                or normalized.replace("_", "") in _FORBIDDEN_SECRET_KEY_FORMS
            ) and child not in (None, "", [], {}):
                return True
            if _contains_forbidden_secret(child):
                return True
    return isinstance(value, list) and any(_contains_forbidden_secret(item) for item in value)


def _restore_max_bytes() -> int:
    raw = str(os.getenv("AGVM_BRAIN_SYNC_RESTORE_MAX_BYTES") or "").strip()
    if not raw:
        return DEFAULT_MAX_RESTORE_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="brain_sync_restore_max_bytes_invalid") from exc
    if value < 1:
        raise HTTPException(status_code=500, detail="brain_sync_restore_max_bytes_invalid")
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="brain_sync_restore_json_invalid") from exc


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _directory_sha256(path: Path) -> str:
    root = path.resolve()
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("brain_sync_restore_directory_invalid")
    digest = hashlib.sha256()
    digest.update(b"agvm.brain_sync.directory.v1\0")
    try:
        entries = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        for entry in entries:
            if entry.is_symlink():
                raise RuntimeError("brain_sync_restore_directory_symlink_forbidden")
            relative = entry.relative_to(root).as_posix().encode("utf-8")
            if entry.is_dir():
                digest.update(b"D\0")
                digest.update(len(relative).to_bytes(8, "big"))
                digest.update(relative)
                continue
            if not entry.is_file():
                raise RuntimeError("brain_sync_restore_directory_entry_invalid")
            digest.update(b"F\0")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            with entry.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
    except (OSError, ValueError) as exc:
        raise RuntimeError("brain_sync_restore_directory_unreadable") from exc
    return f"sha256:{digest.hexdigest()}"


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass


def _require_within_root(path: Path, root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="brain_sync_restore_path_outside_root") from exc
    if resolved_path == resolved_root:
        raise HTTPException(status_code=409, detail="brain_sync_restore_root_forbidden")


def _safe_remove_tree(path: Path, *, required_parent: Path) -> None:
    target = path.resolve()
    parent = required_parent.resolve()
    try:
        target.relative_to(parent)
    except ValueError as exc:
        raise RuntimeError("brain_sync_restore_unsafe_remove_path") from exc
    if target == parent or target.is_symlink():
        raise RuntimeError("brain_sync_restore_unsafe_remove_path")
    if target.exists():
        shutil.rmtree(target)


@contextmanager
def _exclusive_restore_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".brain_sync_restore.lock"
    with _RESTORE_LOCK:
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
