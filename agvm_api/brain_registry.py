# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from config import API_DIR, ATLAS_VERSION, BASE_DIR, DATA_DIR, GRAPH_VERSION, INDEX_VERSION
from storage import utc_timestamp


LOCAL_BRAIN_REGISTRY_SCHEMA_VERSION = "agvm.local_brain_registry.v1"
LOCAL_BRAIN_RECORD_SCHEMA_VERSION = "agvm.local_brain_record.v1"
LOCAL_BRAIN_STORAGE_FORMAT_VERSION = "agvm.local_brain_storage.v1"
IMPORTED_BOOTSTRAP_LIFECYCLE_SCHEMA_VERSION = "agvm.brain_bootstrap_v1.import_lifecycle.v1"

GRAPH_FILENAME = "beta_vector_memory.graph.json"
GRAPH_VIEW_FILENAME = "beta_vector_memory.graph.view.json"
INDEX_FILENAME = "beta_vector_memory.index.json"
ATLAS_FILENAME = "beta_vector_memory.atlas.json"
SQLITE_FILENAME = "beta_vector_memory.sqlite3"

_REGISTRY_WRITE_LOCK = threading.RLock()


class BrainRegistryError(ValueError):
    pass


def brain_root_path() -> Path:
    configured = str(os.getenv("AGVM_BRAINS_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    default_root = (BASE_DIR / "brains").expanduser().resolve()
    if _is_writable_brain_root_candidate(default_root):
        return default_root
    core_home = str(os.getenv("AGVM_CORE_HOME") or "").strip()
    if core_home:
        fallback_root = Path(core_home).expanduser().resolve() / "brains"
        if _is_writable_brain_root_candidate(fallback_root):
            return fallback_root
    local_app_data = str(os.getenv("LOCALAPPDATA") or "").strip()
    if local_app_data:
        fallback_root = Path(local_app_data).expanduser().resolve() / "AGVM" / "brains"
        if _is_writable_brain_root_candidate(fallback_root):
            return fallback_root
    home_fallback = Path.home().expanduser().resolve() / ".agvm" / "brains"
    if _is_writable_brain_root_candidate(home_fallback):
        return home_fallback
    return (Path(tempfile.gettempdir()).expanduser().resolve() / "agvm-core" / "brains")


def _is_writable_brain_root_candidate(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".agvm_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def brain_registry_path(brain_root: Path | None = None) -> Path:
    root = (brain_root or brain_root_path()).resolve()
    return root / "brain_registry.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    with _REGISTRY_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
        try:
            os.close(fd)
            tmp_path = Path(tmp_name)
            tmp_path.write_text(serialized, encoding="utf-8")
            for attempt in range(5):
                try:
                    tmp_path.replace(path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            tmp_path = Path(tmp_name)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


def write_imported_bootstrap_lifecycle_marker(
    *,
    registry_brain_path: Path,
    brain_id: str,
    session_id: str,
    source: str,
    revision: int = 0,
    overwrite: bool = False,
) -> None:
    marker_path = registry_brain_path / "brain_bootstrap_v1" / "import_lifecycle.json"
    if marker_path.exists() and not overwrite:
        return
    _atomic_write_json(
        marker_path,
        {
            "schema_version": IMPORTED_BOOTSTRAP_LIFECYCLE_SCHEMA_VERSION,
            "lifecycle_state": "applied",
            "brain_id": brain_id,
            "session_id": session_id,
            "revision": int(revision),
            "source": source,
            "recorded_at": utc_timestamp(),
        },
    )


def _safe_id(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value or "").strip().lower()).strip("_")
    return value or "brain"


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _safe_rmtree(path: Path, *, required_parent: Path) -> None:
    target = path.resolve()
    parent = required_parent.resolve()
    if target == parent or not _path_is_within(target, parent):
        raise BrainRegistryError(f"unsafe_delete_path:{target}")
    if target.exists():
        shutil.rmtree(target)


def _unique_brain_id(base: str, existing_ids: set[str]) -> str:
    normalized = _safe_id(base)
    if normalized not in existing_ids:
        return normalized
    suffix = 2
    while f"{normalized}_{suffix}" in existing_ids:
        suffix += 1
    return f"{normalized}_{suffix}"


def _legacy_storage_slug(path: Path) -> str:
    name = path.name.strip()
    lower = name.lower()
    if lower == "data":
        configured = str(os.getenv("AGVM_LEGACY_DATA_BRAIN_ID", "") or "").strip()
        return configured or name
    if lower.startswith("data_") and len(name) > 5:
        return name[5:]
    return name


def _known_legacy_brain_id(path: Path) -> str:
    return _safe_id(_legacy_storage_slug(path))


def _known_display_name(path: Path, brain_id: str) -> str:
    configured = str(os.getenv("AGVM_LEGACY_DATA_DISPLAY_NAME", "") or "").strip()
    if path.name.lower() == "data" and configured:
        return configured
    source = _legacy_storage_slug(path) or brain_id
    return _safe_id(source).replace("_", " ").title()


def _json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _json_object_with_error(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.is_file():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, "lifecycle_artifact_unreadable"
    if not isinstance(payload, dict):
        return {}, "lifecycle_artifact_not_object"
    return payload, None


def _benchmark_lifecycle(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "not_reported"
    required = {"weighted_improvement", "max_critical_regression", "p95_latency_ratio"}
    if value.get("complete") is not True or not required.issubset(value):
        return "pending"
    try:
        passed = (
            float(value["weighted_improvement"]) >= 0.05
            and float(value["max_critical_regression"]) <= 0.02
            and float(value["p95_latency_ratio"]) <= 1.20
        )
    except (TypeError, ValueError):
        return "failed"
    return "passed" if passed else "failed"


def _latest_bootstrap_revision(registry_brain_path: Path) -> tuple[dict[str, Any], str | None]:
    sessions_root = registry_brain_path / "brain_bootstrap_v1" / "sessions"
    if not sessions_root.is_dir():
        return {}, None
    revision_paths: list[Path] = []
    try:
        for session_dir in sessions_root.iterdir():
            revisions_dir = session_dir / "revisions"
            if revisions_dir.is_dir():
                revision_paths.extend(path for path in revisions_dir.glob("*.json") if path.is_file())
    except OSError:
        return {}, "bootstrap_state_unreadable"
    if not revision_paths:
        return {}, None
    try:
        latest = max(revision_paths, key=lambda path: (path.stat().st_mtime_ns, path.name))
    except OSError:
        return {}, "bootstrap_state_unreadable"
    return _json_object_with_error(latest)


def _imported_bootstrap_lifecycle(registry_brain_path: Path) -> tuple[dict[str, Any], str | None]:
    marker_path = registry_brain_path / "brain_bootstrap_v1" / "import_lifecycle.json"
    if not marker_path.exists():
        return {}, None
    return _json_object_with_error(marker_path)


def _brain_lifecycle(storage_path: Path, registry_brain_path: Path) -> dict[str, Any]:
    lifecycle: dict[str, Any] = {
        "bootstrap_state": "not_started",
        "profile_state": "not_reported",
        "benchmark_state": "not_reported",
    }
    errors: list[str] = []

    bootstrap, bootstrap_error = _latest_bootstrap_revision(registry_brain_path)
    if bootstrap_error:
        errors.append(bootstrap_error)
        lifecycle["bootstrap_state"] = "error"
    elif bootstrap:
        lifecycle["bootstrap_state"] = str(bootstrap.get("lifecycle_state") or "error")
        lifecycle["bootstrap_session_id"] = str(bootstrap.get("session_id") or "") or None
    else:
        imported_bootstrap, imported_bootstrap_error = _imported_bootstrap_lifecycle(registry_brain_path)
        if imported_bootstrap_error:
            errors.append(imported_bootstrap_error)
            lifecycle["bootstrap_state"] = "error"
        elif imported_bootstrap:
            lifecycle["bootstrap_state"] = str(imported_bootstrap.get("lifecycle_state") or "error")
            lifecycle["bootstrap_session_id"] = str(imported_bootstrap.get("session_id") or "") or None

    profile_state, state_error = _json_object_with_error(storage_path / "brain_profile_v1_api" / "state.json")
    runtime_profile, runtime_error = _json_object_with_error(storage_path / "brain_profile_v1.json")
    if state_error or runtime_error:
        errors.extend(item for item in (state_error, runtime_error) if item)
        lifecycle["profile_state"] = "error"
    else:
        current_revision = int(profile_state.get("current_profile_revision") or 0)
        previous_revision = profile_state.get("previous_revision")
        lifecycle["profile_revision"] = current_revision
        lifecycle["previous_profile_revision"] = int(previous_revision) if previous_revision is not None else None
        runtime_state = str(runtime_profile.get("state") or "").strip().lower()
        if runtime_state == "shadow":
            lifecycle["profile_state"] = "shadow"
        elif runtime_state == "active" and current_revision > 0:
            lifecycle["profile_state"] = "rollback_available" if previous_revision is not None else "active"
        elif runtime_profile:
            lifecycle["profile_state"] = "error"
        lifecycle["benchmark_state"] = _benchmark_lifecycle(runtime_profile.get("benchmark"))

    if errors:
        lifecycle["error_code"] = sorted(set(errors))[0]
    return lifecycle


def _sqlite_node_count(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=1.0)
        try:
            row = conn.execute("SELECT COUNT(*) FROM nodes_nav").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _storage_file_status(storage_path: Path) -> dict[str, Any]:
    statuses: dict[str, Any] = {}
    for filename in (SQLITE_FILENAME, GRAPH_FILENAME, GRAPH_VIEW_FILENAME, INDEX_FILENAME, ATLAS_FILENAME):
        path = storage_path / filename
        statuses[filename] = {
            "exists": path.exists(),
            "path": str(path.resolve()),
            "size_bytes": path.stat().st_size if path.exists() else 0,
        }
    return statuses


def _node_count(storage_path: Path) -> int:
    atlas = _json_file(storage_path / ATLAS_FILENAME)
    if atlas.get("node_count") is not None:
        return int(atlas.get("node_count") or 0)
    graph = _json_file(storage_path / GRAPH_FILENAME)
    if graph.get("nodes") is not None:
        return len(list(graph.get("nodes") or []))
    sqlite_count = _sqlite_node_count(storage_path / SQLITE_FILENAME)
    return int(sqlite_count or 0)


def _ensure_nonempty_import_bootstrap_lifecycle(
    *,
    storage_path: Path,
    registry_brain_path: Path,
    brain_id: str,
) -> None:
    if _node_count(storage_path) <= 0:
        return
    bootstrap, bootstrap_error = _latest_bootstrap_revision(registry_brain_path)
    marker_path = registry_brain_path / "brain_bootstrap_v1" / "import_lifecycle.json"
    if bootstrap or bootstrap_error or marker_path.exists():
        return
    write_imported_bootstrap_lifecycle_marker(
        registry_brain_path=registry_brain_path,
        brain_id=brain_id,
        session_id=f"archive-import:{brain_id}",
        source="nonempty_archive_import",
    )


def _env_truthy(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _repo_legacy_discovery_allowed() -> bool:
    return _env_truthy("AGVM_DISCOVER_REPO_LEGACY_DATA") or _env_truthy("AGVM_ENABLE_LEGACY_DATA_DISCOVERY")


def _storage_quality(storage_path: Path) -> dict[str, Any]:
    storage = storage_path.resolve()
    file_status = _storage_file_status(storage)
    has_sqlite = bool(file_status[SQLITE_FILENAME]["exists"])
    has_index = bool(file_status[INDEX_FILENAME]["exists"])
    has_atlas = bool(file_status[ATLAS_FILENAME]["exists"])
    node_count = _node_count(storage) if has_sqlite or has_index or has_atlas else 0
    sqlite_size = int(file_status[SQLITE_FILENAME]["size_bytes"])
    return {
        "path": str(storage),
        "has_sqlite": has_sqlite,
        "has_index": has_index,
        "has_atlas": has_atlas,
        "node_count": int(node_count or 0),
        "sqlite_size_bytes": sqlite_size,
        "safe_for_mcp": bool(has_sqlite and has_index and has_atlas and node_count > 0),
    }


def _looks_like_foreign_runtime_path(raw_path: str) -> bool:
    normalized = str(raw_path or "").strip()
    if not normalized:
        return False
    if os.name == "nt":
        return normalized.startswith("/app/") or normalized.startswith("/home/")
    return bool(re.search(r"(?:^|[/\\])[a-zA-Z]:[\\/]", normalized))


def _portable_registered_storage_path(
    record: dict[str, Any] | None,
    *,
    brain_root: Path,
    brain_id: str,
) -> Path:
    payload = dict(record or {})
    registry_brain = (brain_root / _safe_id(brain_id)).resolve()
    managed_storage = registry_brain / "storage"
    in_place_storage = registry_brain
    raw = str(payload.get("storage_path") or "").strip()
    raw_path = Path(raw).expanduser() if raw else None
    local_candidates = (managed_storage, in_place_storage)
    for candidate in local_candidates:
        if any((candidate / filename).is_file() for filename in (SQLITE_FILENAME, GRAPH_FILENAME, INDEX_FILENAME, ATLAS_FILENAME)):
            if raw_path is None or not raw_path.exists() or _looks_like_foreign_runtime_path(raw):
                return candidate.resolve()
    if raw_path is not None and raw_path.exists() and not _looks_like_foreign_runtime_path(raw):
        return raw_path.resolve()
    if managed_storage.exists() or str(payload.get("storage_layout") or "") == "registry_managed":
        return managed_storage.resolve()
    return raw_path.resolve() if raw_path is not None else managed_storage.resolve()


def _portable_registered_brain_path(
    record: dict[str, Any] | None,
    *,
    brain_root: Path,
    brain_id: str,
) -> Path:
    payload = dict(record or {})
    managed_path = (brain_root / _safe_id(brain_id)).resolve()
    raw = str(payload.get("registry_brain_path") or "").strip()
    raw_path = Path(raw).expanduser() if raw else None
    if managed_path.exists() or raw_path is None or not raw_path.exists() or _looks_like_foreign_runtime_path(raw):
        return managed_path
    return raw_path.resolve()


def _prefer_previous_storage(previous: dict[str, Any] | None, discovered_storage: Path) -> Path | None:
    if not previous:
        return None
    previous_raw = str(previous.get("storage_path") or "").strip()
    if not previous_raw:
        return None
    previous_storage = Path(previous_raw).expanduser().resolve()
    discovered = discovered_storage.resolve()
    if previous_storage == discovered or not previous_storage.exists():
        return None
    previous_quality = _storage_quality(previous_storage)
    discovered_quality = _storage_quality(discovered)
    previous_is_real = bool(previous_quality["safe_for_mcp"]) and int(previous_quality["node_count"]) > 0
    discovered_is_weaker = (
        not bool(discovered_quality["safe_for_mcp"])
        or int(discovered_quality["node_count"]) < int(previous_quality["node_count"])
        or int(discovered_quality["sqlite_size_bytes"]) <= 4096
    )
    if previous_is_real and discovered_is_weaker:
        return previous_storage
    return None


def discover_legacy_data_dirs(
    *,
    api_dir: Path | None = None,
    current_data_dir: Path | None = None,
) -> list[Path]:
    api = (api_dir or API_DIR).resolve()
    current = (current_data_dir or DATA_DIR).resolve()
    explicit_brain_root = bool(str(os.getenv("AGVM_BRAINS_DIR") or "").strip())
    explicit_current_data = current_data_dir is not None or bool(str(os.getenv("AGVM_LAB_DATA_DIR") or "").strip())
    candidates: list[Path] = []
    if not explicit_brain_root or _repo_legacy_discovery_allowed():
        candidates.append(api / "data")
        try:
            candidates.extend(sorted(path for path in api.glob("data_*") if path.is_dir()))
        except OSError:
            pass
    if explicit_current_data or not explicit_brain_root:
        candidates.append(current)
    discovered: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = candidate.resolve()
        if str(path) in seen:
            continue
        seen.add(str(path))
        if any((path / filename).exists() for filename in (SQLITE_FILENAME, GRAPH_FILENAME, INDEX_FILENAME, ATLAS_FILENAME)):
            discovered.append(path)
    return discovered


def _ensure_brain_dirs(registry_brain_path: Path) -> dict[str, str]:
    registry_brain_path.mkdir(parents=True, exist_ok=True)
    paths = {
        "registry_brain_path": registry_brain_path,
        "document_asset_path": registry_brain_path / "documents",
        "source_package_path": registry_brain_path / "source_packages",
        "maintenance_path": registry_brain_path / "maintenance",
        "mcp_log_path": registry_brain_path / "mcp_logs",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return {key: str(path.resolve()) for key, path in paths.items()}


def build_local_brain_record(
    *,
    brain_id: str,
    display_name: str,
    storage_path: Path,
    registry_brain_path: Path,
    is_default: bool = False,
    is_active: bool = False,
    migration_source: str = "legacy_data_dir",
    storage_layout: str = "legacy_in_place",
    migration_status: str = "legacy_imported_in_place_runtime_scoped_pr12m_b",
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous_payload = dict(previous or {})
    storage = storage_path.resolve()
    storage.mkdir(parents=True, exist_ok=True)
    registry_paths = _ensure_brain_dirs(registry_brain_path.resolve())
    registry_brain = Path(registry_paths["registry_brain_path"])
    file_status = _storage_file_status(storage)
    has_sqlite = bool(file_status[SQLITE_FILENAME]["exists"])
    has_index = bool(file_status[INDEX_FILENAME]["exists"])
    has_atlas = bool(file_status[ATLAS_FILENAME]["exists"])
    warnings: list[str] = []
    if not has_sqlite:
        warnings.append("sqlite_store_missing")
    if not has_index:
        warnings.append("index_file_missing")
    if not has_atlas:
        warnings.append("atlas_file_missing")
    return {
        "schema_version": LOCAL_BRAIN_RECORD_SCHEMA_VERSION,
        "brain_id": brain_id,
        "display_name": display_name,
        "description": previous_payload.get("description") or f"Imported local AGVM brain: {display_name}.",
        "storage_path": str(storage),
        "storage_layout": storage_layout,
        "registry_brain_path": registry_paths["registry_brain_path"],
        "document_asset_path": registry_paths["document_asset_path"],
        "source_package_path": registry_paths["source_package_path"],
        "maintenance_path": registry_paths["maintenance_path"],
        "mcp_log_path": registry_paths["mcp_log_path"],
        "storage_format_version": LOCAL_BRAIN_STORAGE_FORMAT_VERSION,
        "graph_version": GRAPH_VERSION,
        "index_version": INDEX_VERSION,
        "atlas_version": ATLAS_VERSION,
        "migration_status": migration_status,
        "migration_source": migration_source,
        "migration_target_path": registry_paths["registry_brain_path"],
        "created_at": previous_payload.get("created_at") or utc_timestamp(),
        "updated_at": utc_timestamp(),
        "is_default": bool(is_default),
        "is_active": bool(is_active),
        "safe_for_mcp": has_sqlite and has_index and has_atlas and _node_count(storage) > 0,
        "runtime_scope_status": "brain_scoped_runtime_ready_pr12m_b",
        "node_count": _node_count(storage),
        "lifecycle": _brain_lifecycle(storage, registry_brain),
        "sqlite_size_bytes": int(file_status[SQLITE_FILENAME]["size_bytes"]),
        "storage_files": file_status,
        "capabilities": {
            "retrieve": has_sqlite and _node_count(storage) > 0,
            "grow": has_sqlite,
            "documents": True,
            "source_packages": True,
            "maintenance": has_sqlite,
            "mcp_logs": True,
        },
        "warnings": warnings,
    }


def validate_local_brain_registry(registry: dict[str, Any]) -> dict[str, Any]:
    brains = [dict(item) for item in list(registry.get("brains") or []) if isinstance(item, dict)]
    ids = [str(item.get("brain_id") or "").strip() for item in brains]
    unique_ids = sorted(set(ids))
    duplicate_ids = sorted({brain_id for brain_id in ids if ids.count(brain_id) > 1})
    default_ids = [str(item.get("brain_id") or "") for item in brains if bool(item.get("is_default"))]
    active_ids = [str(item.get("brain_id") or "") for item in brains if bool(item.get("is_active"))]
    missing_paths: list[str] = []
    shared_storage_paths: list[str] = []
    storage_paths = [str(item.get("storage_path") or "") for item in brains]
    for item in brains:
        for field in ("storage_path", "registry_brain_path", "document_asset_path", "source_package_path", "maintenance_path", "mcp_log_path"):
            raw = str(item.get(field) or "")
            if not raw or not Path(raw).exists():
                missing_paths.append(f"{item.get('brain_id')}:{field}")
    for path in sorted(set(storage_paths)):
        if path and storage_paths.count(path) > 1:
            shared_storage_paths.append(path)
    errors: list[str] = []
    if registry.get("schema_version") != LOCAL_BRAIN_REGISTRY_SCHEMA_VERSION:
        errors.append("registry_schema_version_mismatch")
    if not brains:
        errors.append("no_brains_registered")
    if duplicate_ids:
        errors.append("duplicate_brain_ids")
    if len(default_ids) != 1:
        errors.append("default_brain_count_not_one")
    if len(active_ids) != 1:
        errors.append("active_brain_count_not_one")
    if missing_paths:
        errors.append("registered_paths_missing")
    if shared_storage_paths:
        errors.append("shared_storage_path_between_brains")
    safe_default_configured = len(default_ids) == 1 and default_ids[0] in unique_ids
    if len(brains) > 1 and not safe_default_configured:
        errors.append("multi_brain_without_safe_default")
    return {
        "schema_version": "agvm.local_brain_registry.validation.v1",
        "passed": not errors,
        "brain_count": len(brains),
        "brain_ids": unique_ids,
        "default_brain_ids": default_ids,
        "active_brain_ids": active_ids,
        "duplicate_brain_ids": duplicate_ids,
        "missing_paths": missing_paths,
        "shared_storage_paths": shared_storage_paths,
        "safe_default_configured": safe_default_configured,
        "errors": errors,
        "next_slice": "PR-12N-B Cloud Persistence And Operations",
    }


def _normalize_active_default(brains: list[dict[str, Any]], preferred_id: str | None = None) -> list[dict[str, Any]]:
    if not brains:
        return []
    ids = [str(item.get("brain_id") or "") for item in brains]
    preferred = str(preferred_id or "").strip()
    current_default = next((str(item.get("brain_id") or "") for item in brains if bool(item.get("is_default"))), "")
    current_active = next((str(item.get("brain_id") or "") for item in brains if bool(item.get("is_active"))), "")
    target = preferred if preferred in ids else current_active if current_active in ids else current_default if current_default in ids else ids[0]
    normalized: list[dict[str, Any]] = []
    for item in brains:
        row = dict(item)
        row["is_default"] = str(row.get("brain_id") or "") == target
        row["is_active"] = str(row.get("brain_id") or "") == target
        normalized.append(row)
    return normalized


def bootstrap_local_brain_registry(
    *,
    brain_root: Path | None = None,
    legacy_data_dirs: list[Path] | None = None,
    current_data_dir: Path | None = None,
    preferred_default_brain_id: str | None = None,
) -> dict[str, Any]:
    root = (brain_root or brain_root_path()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    registry_file = brain_registry_path(root)
    existing = _json_file(registry_file)
    previous_by_id = {
        str(item.get("brain_id") or ""): dict(item)
        for item in list(existing.get("brains") or [])
        if isinstance(item, dict) and str(item.get("brain_id") or "").strip()
    }
    data_dirs = discover_legacy_data_dirs(current_data_dir=current_data_dir) if legacy_data_dirs is None else legacy_data_dirs
    current = (current_data_dir or DATA_DIR).resolve()
    env_default = str(os.getenv("AGVM_DEFAULT_BRAIN_ID", "") or "").strip()
    requested_preferred = str(preferred_default_brain_id or env_default or "").strip()
    preferred = requested_preferred
    known_preferred_ids = set(previous_by_id.keys())
    known_preferred_ids.update(_known_legacy_brain_id(path.resolve()) for path in data_dirs)
    if preferred and preferred not in known_preferred_ids:
        preferred = ""
    if not preferred:
        existing_active = str(existing.get("active_brain_id") or "").strip()
        existing_default = str(existing.get("default_brain_id") or "").strip()
        if existing_active and existing_active in previous_by_id:
            preferred = existing_active
        elif existing_default and existing_default in previous_by_id:
            preferred = existing_default
    if not preferred:
        for candidate in data_dirs:
            if candidate.resolve() == current:
                preferred = _known_legacy_brain_id(candidate)
                break
    brain_records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for data_dir in data_dirs:
        discovered_storage_path = data_dir.resolve()
        brain_id = _known_legacy_brain_id(discovered_storage_path)
        if brain_id in seen_ids:
            suffix = 2
            base = brain_id
            while f"{base}_{suffix}" in seen_ids:
                suffix += 1
            brain_id = f"{base}_{suffix}"
        seen_ids.add(brain_id)
        previous = previous_by_id.get(brain_id)
        storage_path = _prefer_previous_storage(previous, discovered_storage_path) or discovered_storage_path
        brain_records.append(
            build_local_brain_record(
                brain_id=brain_id,
                display_name=str((previous or {}).get("display_name") or _known_display_name(discovered_storage_path, brain_id)),
                storage_path=storage_path,
                registry_brain_path=root / brain_id,
                is_default=bool((previous or {}).get("is_default")),
                is_active=bool((previous or {}).get("is_active")),
                migration_source=str((previous or {}).get("migration_source") or "legacy_data_dir"),
                storage_layout=str((previous or {}).get("storage_layout") or "legacy_in_place"),
                migration_status=str((previous or {}).get("migration_status") or "legacy_imported_in_place_runtime_scoped_pr12m_b"),
                previous=previous,
            )
        )
    for brain_id, previous in previous_by_id.items():
        if brain_id not in seen_ids and str(previous.get("storage_path") or "").strip():
            storage_path = _portable_registered_storage_path(previous, brain_root=root, brain_id=brain_id)
            registry_brain_path = _portable_registered_brain_path(previous, brain_root=root, brain_id=brain_id)
            brain_records.append(
                build_local_brain_record(
                    brain_id=brain_id,
                    display_name=str(previous.get("display_name") or brain_id),
                    storage_path=storage_path,
                    registry_brain_path=registry_brain_path,
                    is_default=False,
                    is_active=False,
                    migration_source=str(previous.get("migration_source") or "existing_registry"),
                    previous=previous,
                )
            )
    if not brain_records:
        default_brain_id = _safe_id(requested_preferred or preferred or os.getenv("AGVM_DEFAULT_BRAIN_ID") or "default_brain")
        default_brain_path = root / default_brain_id
        brain_records.append(
            build_local_brain_record(
                brain_id=default_brain_id,
                display_name=_known_display_name(default_brain_path, default_brain_id),
                storage_path=default_brain_path / "storage",
                registry_brain_path=default_brain_path,
                is_default=False,
                is_active=False,
                migration_source="empty_self_hosted_default",
                storage_layout="registry_managed",
                migration_status="empty_local_brain_ready_for_runtime_bootstrap_pr12m_d",
            )
        )
    brain_records = _normalize_active_default(brain_records, preferred_id=preferred)
    registry = {
        "schema_version": LOCAL_BRAIN_REGISTRY_SCHEMA_VERSION,
        "registry_id": "local",
        "brain_root": str(root),
        "registry_path": str(registry_file),
        "storage_format_version": LOCAL_BRAIN_STORAGE_FORMAT_VERSION,
        "created_at": existing.get("created_at") or utc_timestamp(),
        "updated_at": utc_timestamp(),
        "active_brain_id": next((str(item.get("brain_id") or "") for item in brain_records if bool(item.get("is_active"))), ""),
        "default_brain_id": next((str(item.get("brain_id") or "") for item in brain_records if bool(item.get("is_default"))), ""),
        "brain_count": len(brain_records),
        "legacy_data_dir_policy": {
            "agvm_lab_data_dir": str(DATA_DIR.resolve()),
            "role": "backward_compatible_import_default_only",
            "product_switching": "brain_registry",
        },
        "runtime_scope_status": "brain_scoped_runtime_ready_pr12m_b",
        "brains": brain_records,
        "product_boundary": {
            "implemented_slice": "PR-12N-A Hosted Brain Registry And Tenant Isolation",
            "registry_slice": "PR-12M-A Local Brain Registry",
            "runtime_scoping_slice": "PR-12M-B Brain-Scoped Runtime",
            "mcp_server_slice": "PR-12M-C Local MCP Server Package",
            "self_hosted_distribution_slice": "PR-12M-D Docker Self-Hosted Distribution",
            "local_admin_export_slice": "PR-12M-E Local Admin And Export",
            "self_hosted_readiness_slice": "PR-12M-F Self-Hosted Readiness Benchmark",
            "hosted_registry_slice": "PR-12N-A Hosted Brain Registry And Tenant Isolation",
            "self_hosted_release_status": "self_hosted_ready_pr12m_closed",
            "cloud_commercialization_status": "hosted_registry_ready_cloud_persistence_pending",
            "data_copy_policy": "existing_large_brains_imported_in_place_no_db_copy",
            "agvm_lab_data_dir_role": "backward_compatible_import_default_only",
        },
        "next_slice": "PR-12N-B Cloud Persistence And Operations",
    }
    registry["validation"] = validate_local_brain_registry(registry)
    _atomic_write_json(registry_file, registry)
    return registry


def _finalize_registry(registry: dict[str, Any], *, brain_root: Path | None = None) -> dict[str, Any]:
    brains = [dict(item) for item in list(registry.get("brains") or []) if isinstance(item, dict)]
    registry["brains"] = brains
    registry["brain_count"] = len(brains)
    registry["active_brain_id"] = next((str(item.get("brain_id") or "") for item in brains if bool(item.get("is_active"))), "")
    registry["default_brain_id"] = next((str(item.get("brain_id") or "") for item in brains if bool(item.get("is_default"))), "")
    registry["updated_at"] = utc_timestamp()
    product_boundary = dict(registry.get("product_boundary") or {})
    product_boundary.update(
        {
            "implemented_slice": "PR-12N-A Hosted Brain Registry And Tenant Isolation",
            "local_admin_export_slice": "PR-12M-E Local Admin And Export",
            "self_hosted_readiness_slice": "PR-12M-F Self-Hosted Readiness Benchmark",
            "hosted_registry_slice": "PR-12N-A Hosted Brain Registry And Tenant Isolation",
            "self_hosted_release_status": "self_hosted_ready_pr12m_closed",
            "cloud_commercialization_status": "hosted_registry_ready_cloud_persistence_pending",
        }
    )
    registry["product_boundary"] = product_boundary
    registry["next_slice"] = "PR-12N-B Cloud Persistence And Operations"
    registry["validation"] = validate_local_brain_registry(registry)
    _atomic_write_json(brain_registry_path(brain_root), registry)
    return registry


def refresh_local_brain_registry(*, brain_root: Path | None = None) -> dict[str, Any]:
    root = (brain_root or brain_root_path()).resolve()
    registry = load_local_brain_registry(brain_root=root)
    refreshed: list[dict[str, Any]] = []
    for previous in list(registry.get("brains") or []):
        if not isinstance(previous, dict):
            continue
        brain_id = str(previous.get("brain_id") or "").strip()
        if not brain_id:
            continue
        storage_path = _portable_registered_storage_path(previous, brain_root=root, brain_id=brain_id)
        registry_brain_path = _portable_registered_brain_path(previous, brain_root=root, brain_id=brain_id)
        refreshed.append(
            build_local_brain_record(
                brain_id=brain_id,
                display_name=str(previous.get("display_name") or brain_id),
                storage_path=storage_path,
                registry_brain_path=registry_brain_path,
                is_default=bool(previous.get("is_default")),
                is_active=bool(previous.get("is_active")),
                migration_source=str(previous.get("migration_source") or "existing_registry"),
                storage_layout=str(previous.get("storage_layout") or "registry_managed"),
                migration_status=str(previous.get("migration_status") or "registry_refreshed_pr12m_e"),
                previous=previous,
            )
        )
    registry["brains"] = refreshed
    return _finalize_registry(registry, brain_root=root)


def refresh_local_brain_record(
    brain_id: str,
    *,
    brain_root: Path | None = None,
    minimum_node_count: int = 0,
    expected_bootstrap_state: str | None = None,
    expected_bootstrap_session_id: str | None = None,
) -> dict[str, Any]:
    target = str(brain_id or "").strip()
    if not target:
        raise BrainRegistryError("brain_id_required")
    root = (brain_root or brain_root_path()).resolve()
    with _REGISTRY_WRITE_LOCK:
        registry = load_local_brain_registry(brain_root=root)
        brains = [dict(item) for item in list(registry.get("brains") or []) if isinstance(item, dict)]
        target_index = next(
            (index for index, item in enumerate(brains) if str(item.get("brain_id") or "") == target),
            None,
        )
        if target_index is None:
            raise BrainRegistryError(f"unknown_brain_id:{target}")
        previous = brains[target_index]
        storage_path = _portable_registered_storage_path(previous, brain_root=root, brain_id=target)
        registry_brain_path = _portable_registered_brain_path(previous, brain_root=root, brain_id=target)
        refreshed = build_local_brain_record(
            brain_id=target,
            display_name=str(previous.get("display_name") or target),
            storage_path=storage_path,
            registry_brain_path=registry_brain_path,
            is_default=bool(previous.get("is_default")),
            is_active=bool(previous.get("is_active")),
            migration_source=str(previous.get("migration_source") or "existing_registry"),
            storage_layout=str(previous.get("storage_layout") or "registry_managed"),
            migration_status=str(previous.get("migration_status") or "registry_refreshed_pr12m_e"),
            previous=previous,
        )
        if int(refreshed.get("node_count") or 0) < max(0, int(minimum_node_count)):
            raise BrainRegistryError("brain_registry_refresh_node_count_not_persisted")
        lifecycle = dict(refreshed.get("lifecycle") or {})
        if expected_bootstrap_state is not None and lifecycle.get("bootstrap_state") != expected_bootstrap_state:
            raise BrainRegistryError("brain_registry_refresh_bootstrap_state_mismatch")
        if (
            expected_bootstrap_session_id is not None
            and lifecycle.get("bootstrap_session_id") != expected_bootstrap_session_id
        ):
            raise BrainRegistryError("brain_registry_refresh_bootstrap_session_mismatch")
        brains[target_index] = refreshed
        registry["brains"] = brains
        _finalize_registry(registry, brain_root=root)
        return dict(refreshed)


def create_local_brain(
    *,
    display_name: str,
    brain_id: str | None = None,
    description: str | None = None,
    make_default: bool = False,
    make_active: bool = True,
    brain_root: Path | None = None,
) -> dict[str, Any]:
    root = (brain_root or brain_root_path()).resolve()
    registry = load_local_brain_registry(brain_root=root)
    brains = [dict(item) for item in list(registry.get("brains") or []) if isinstance(item, dict)]
    existing_ids = {str(item.get("brain_id") or "") for item in brains}
    target_id = _unique_brain_id(brain_id or display_name or "brain", existing_ids)
    registry_brain_path = root / target_id
    record = build_local_brain_record(
        brain_id=target_id,
        display_name=str(display_name or target_id.replace("_", " ").title()).strip(),
        storage_path=registry_brain_path / "storage",
        registry_brain_path=registry_brain_path,
        is_default=False,
        is_active=False,
        migration_source="local_admin_create",
        storage_layout="registry_managed",
        migration_status="created_by_local_admin_pending_runtime_bootstrap_pr12m_e",
    )
    if description is not None:
        record["description"] = str(description).strip()
    if make_active or make_default or not brains:
        for item in brains:
            item["is_active"] = False if make_active or not brains else bool(item.get("is_active"))
            if make_default or not brains:
                item["is_default"] = False
        record["is_active"] = bool(make_active or not brains)
        record["is_default"] = bool(make_default or not brains)
    brains.append(record)
    registry["brains"] = brains
    return _finalize_registry(registry, brain_root=root)


def rename_local_brain(
    brain_id: str,
    *,
    display_name: str,
    description: str | None = None,
    brain_root: Path | None = None,
) -> dict[str, Any]:
    target = str(brain_id or "").strip()
    registry = load_local_brain_registry(brain_root=brain_root)
    found = False
    brains: list[dict[str, Any]] = []
    for item in list(registry.get("brains") or []):
        row = dict(item)
        if str(row.get("brain_id") or "") == target:
            found = True
            row["display_name"] = str(display_name or target).strip()
            if description is not None:
                row["description"] = str(description).strip()
            row["updated_at"] = utc_timestamp()
        brains.append(row)
    if not found:
        raise BrainRegistryError(f"unknown_brain_id:{target}")
    registry["brains"] = brains
    return _finalize_registry(registry, brain_root=brain_root)


def _brain_export_graph_summary(record: dict[str, Any]) -> dict[str, Any]:
    storage = Path(str(record.get("storage_path") or "")).expanduser()
    graph_path = storage / GRAPH_FILENAME
    index_path = storage / INDEX_FILENAME
    atlas_path = storage / ATLAS_FILENAME
    sqlite_path = storage / SQLITE_FILENAME
    graph = _json_file(graph_path)
    atlas = _json_file(atlas_path)
    graph_nodes = list(graph.get("nodes") or []) if isinstance(graph.get("nodes"), list) else []
    graph_edges = list(graph.get("edges") or []) if isinstance(graph.get("edges"), list) else []
    atlas_node_count = atlas.get("node_count")
    sqlite_node_count = _sqlite_node_count(sqlite_path) if sqlite_path.exists() else None
    runtime_node_count = (
        int(sqlite_node_count)
        if sqlite_node_count is not None
        else int(atlas_node_count or len(graph_nodes) or record.get("node_count") or 0)
    )
    return {
        "schema_version": "agvm.local_brain_graph_export_summary.v1",
        "runtime_node_count": runtime_node_count,
        "graph_payload_node_count": len(graph_nodes),
        "graph_payload_edge_count": len(graph_edges),
        "atlas_node_count": int(atlas_node_count or 0),
        "graph_version": graph.get("version") or record.get("graph_version"),
        "index_version": record.get("index_version"),
        "atlas_version": atlas.get("version") or record.get("atlas_version"),
        "included_files": {
            GRAPH_FILENAME: {
                "path": f"storage/{GRAPH_FILENAME}",
                "present": graph_path.exists(),
                "size_bytes": graph_path.stat().st_size if graph_path.exists() else 0,
            },
            INDEX_FILENAME: {
                "path": f"storage/{INDEX_FILENAME}",
                "present": index_path.exists(),
                "size_bytes": index_path.stat().st_size if index_path.exists() else 0,
            },
            ATLAS_FILENAME: {
                "path": f"storage/{ATLAS_FILENAME}",
                "present": atlas_path.exists(),
                "size_bytes": atlas_path.stat().st_size if atlas_path.exists() else 0,
            },
            SQLITE_FILENAME: {
                "path": f"storage/{SQLITE_FILENAME}",
                "present": sqlite_path.exists(),
                "size_bytes": sqlite_path.stat().st_size if sqlite_path.exists() else 0,
            },
        },
    }


def _brain_export_manifest(record: dict[str, Any], *, export_kind: str) -> dict[str, Any]:
    return {
        "schema_version": "agvm.local_brain_export.v1",
        "export_kind": export_kind,
        "exported_at": utc_timestamp(),
        "brain_id": record.get("brain_id"),
        "display_name": record.get("display_name"),
        "description": record.get("description"),
        "storage_layout": record.get("storage_layout"),
        "storage_format_version": record.get("storage_format_version"),
        "source_registry_path": record.get("registry_brain_path"),
        "source_storage_path": record.get("storage_path"),
        "included_paths": {
            "storage": "storage/",
            "documents": "documents/",
            "source_packages": "source_packages/",
            "maintenance": "maintenance/",
            "mcp_logs": "mcp_logs/",
            "brain_bootstrap_v1": "brain_bootstrap_v1/",
        },
        "graph_export": _brain_export_graph_summary(record),
        "restore_policy": "imports_as_registry_managed_brain_by_default",
    }


def _zip_dir(zip_file: zipfile.ZipFile, source: Path, arc_prefix: str) -> int:
    if not source.exists():
        return 0
    count = 0
    if source.is_file():
        zip_file.write(source, arc_prefix.rstrip("/"))
        return 1
    for path in sorted(source.rglob("*")):
        if path.is_file():
            zip_file.write(path, f"{arc_prefix.rstrip('/')}/{path.relative_to(source).as_posix()}")
            count += 1
    return count


def export_local_brain(
    brain_id: str,
    *,
    export_dir: Path | None = None,
    export_kind: str = "export",
    brain_root: Path | None = None,
) -> dict[str, Any]:
    registry = refresh_local_brain_registry(brain_root=brain_root)
    target = str(brain_id or "").strip()
    record = next((dict(item) for item in list(registry.get("brains") or []) if str(item.get("brain_id") or "") == target), None)
    if not record:
        raise BrainRegistryError(f"unknown_brain_id:{target}")
    root = Path(str(registry.get("brain_root") or brain_root_path())).expanduser().resolve()
    out_dir = (export_dir or root / "exports").expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / f"{target}-{utc_timestamp().replace(':', '').replace('+', 'Z')}.agvm-brain.zip"
    manifest = _brain_export_manifest(record, export_kind=export_kind)
    file_count = 0
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("agvm_brain_export_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zip_file.writestr("brain_record.json", json.dumps(record, ensure_ascii=False, indent=2))
        file_count += _zip_dir(zip_file, Path(str(record.get("storage_path") or "")).expanduser(), "storage")
        file_count += _zip_dir(zip_file, Path(str(record.get("document_asset_path") or "")).expanduser(), "documents")
        file_count += _zip_dir(zip_file, Path(str(record.get("source_package_path") or "")).expanduser(), "source_packages")
        file_count += _zip_dir(zip_file, Path(str(record.get("maintenance_path") or "")).expanduser(), "maintenance")
        file_count += _zip_dir(zip_file, Path(str(record.get("mcp_log_path") or "")).expanduser(), "mcp_logs")
        file_count += _zip_dir(
            zip_file,
            Path(str(record.get("registry_brain_path") or "")).expanduser() / "brain_bootstrap_v1",
            "brain_bootstrap_v1",
        )
    return {
        "schema_version": "agvm.local_brain_export_result.v1",
        "action": export_kind,
        "status": "exported",
        "brain_id": target,
        "archive_path": str(archive_path),
        "archive_size_bytes": archive_path.stat().st_size,
        "file_count": file_count,
        "export_manifest": manifest,
        "registry": registry,
        "warnings": [],
        "next_slice": "PR-12N-B Cloud Persistence And Operations",
    }


def _safe_extract_zip(archive_path: Path, target_dir: Path) -> None:
    archive = archive_path.expanduser().resolve()
    target = target_dir.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as zip_file:
        for member in zip_file.infolist():
            destination = target / member.filename
            if not _path_is_within(destination, target):
                raise BrainRegistryError(f"unsafe_archive_member:{member.filename}")
        zip_file.extractall(target)


def import_local_brain_archive(
    archive_path: Path,
    *,
    brain_id: str | None = None,
    display_name: str | None = None,
    make_active: bool = False,
    make_default: bool = False,
    overwrite_existing: bool = False,
    brain_root: Path | None = None,
) -> dict[str, Any]:
    root = (brain_root or brain_root_path()).resolve()
    registry = load_local_brain_registry(brain_root=root)
    brains = [dict(item) for item in list(registry.get("brains") or []) if isinstance(item, dict)]
    existing_ids = {str(item.get("brain_id") or "") for item in brains}
    with tempfile.TemporaryDirectory(prefix="agvm-brain-import-") as tmp_name:
        temp_dir = Path(tmp_name)
        _safe_extract_zip(archive_path, temp_dir)
        manifest_path = temp_dir / "agvm_brain_export_manifest.json"
        record_path = temp_dir / "brain_record.json"
        manifest = _json_file(manifest_path)
        source_record = _json_file(record_path)
        requested_id = brain_id or str(manifest.get("brain_id") or source_record.get("brain_id") or "imported_brain")
        target_id = _safe_id(requested_id)
        if target_id in existing_ids and not overwrite_existing:
            raise BrainRegistryError(f"brain_id_already_exists:{target_id}")
        target_path = root / target_id
        if target_id in existing_ids and overwrite_existing:
            existing = next((item for item in brains if str(item.get("brain_id") or "") == target_id), {})
            if str(existing.get("storage_layout") or "") != "registry_managed":
                raise BrainRegistryError(f"overwrite_requires_registry_managed_brain:{target_id}")
            _safe_rmtree(target_path, required_parent=root)
            brains = [item for item in brains if str(item.get("brain_id") or "") != target_id]
        target_path.mkdir(parents=True, exist_ok=True)
        for dirname in ("storage", "documents", "source_packages", "maintenance", "mcp_logs", "brain_bootstrap_v1"):
            source = temp_dir / dirname
            destination = target_path / ("storage" if dirname == "storage" else dirname)
            if source.exists():
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(source, destination)
            else:
                destination.mkdir(parents=True, exist_ok=True)
        _ensure_nonempty_import_bootstrap_lifecycle(
            storage_path=target_path / "storage",
            registry_brain_path=target_path,
            brain_id=target_id,
        )
        record = build_local_brain_record(
            brain_id=target_id,
            display_name=str(display_name or manifest.get("display_name") or source_record.get("display_name") or target_id.replace("_", " ").title()),
            storage_path=target_path / "storage",
            registry_brain_path=target_path,
            is_default=False,
            is_active=False,
            migration_source="local_admin_import",
            storage_layout="registry_managed",
            migration_status="imported_from_export_pr12m_e",
            previous={
                "description": source_record.get("description") or manifest.get("description"),
                "created_at": source_record.get("created_at"),
            },
        )
        for item in brains:
            if make_active:
                item["is_active"] = False
            if make_default:
                item["is_default"] = False
        record["is_active"] = bool(make_active or not brains)
        record["is_default"] = bool(make_default or not brains)
        brains.append(record)
        registry["brains"] = brains
        registry = _finalize_registry(registry, brain_root=root)
    return {
        "schema_version": "agvm.local_brain_import_result.v1",
        "action": "import",
        "status": "imported",
        "brain_id": target_id,
        "brain": record,
        "import_manifest": manifest,
        "registry": registry,
        "warnings": [],
        "next_slice": "PR-12N-B Cloud Persistence And Operations",
    }


def delete_local_brain(
    brain_id: str,
    *,
    confirm_brain_id: str,
    delete_storage: bool = False,
    brain_root: Path | None = None,
) -> dict[str, Any]:
    target = str(brain_id or "").strip()
    confirmation = str(confirm_brain_id or "").strip()
    if target != confirmation:
        raise BrainRegistryError("delete_confirmation_mismatch")
    root = (brain_root or brain_root_path()).resolve()
    registry = load_local_brain_registry(brain_root=root)
    brains = [dict(item) for item in list(registry.get("brains") or []) if isinstance(item, dict)]
    record = next((dict(item) for item in brains if str(item.get("brain_id") or "") == target), None)
    if not record:
        raise BrainRegistryError(f"unknown_brain_id:{target}")
    if len(brains) <= 1:
        raise BrainRegistryError("cannot_delete_last_brain")
    if bool(record.get("is_active")) or bool(record.get("is_default")):
        raise BrainRegistryError("cannot_delete_active_or_default_brain")
    warnings: list[str] = []
    if delete_storage:
        if str(record.get("storage_layout") or "") == "registry_managed":
            registry_path = Path(str(record.get("registry_brain_path") or root / target)).expanduser().resolve()
            _safe_rmtree(registry_path, required_parent=root)
        else:
            warnings.append("legacy_in_place_storage_not_deleted")
            registry_path = Path(str(record.get("registry_brain_path") or root / target)).expanduser().resolve()
            if _path_is_within(registry_path, root) and registry_path.exists():
                _safe_rmtree(registry_path, required_parent=root)
    registry["brains"] = [item for item in brains if str(item.get("brain_id") or "") != target]
    registry = _finalize_registry(registry, brain_root=root)
    return {
        "schema_version": "agvm.local_brain_delete_result.v1",
        "action": "delete",
        "status": "deleted",
        "brain_id": target,
        "deleted_storage": bool(delete_storage and str(record.get("storage_layout") or "") == "registry_managed"),
        "registry": registry,
        "warnings": warnings,
        "next_slice": "PR-12N-B Cloud Persistence And Operations",
    }


def load_local_brain_registry(*, brain_root: Path | None = None, bootstrap_if_missing: bool = True) -> dict[str, Any]:
    with _REGISTRY_WRITE_LOCK:
        registry_file = brain_registry_path(brain_root)
        if not registry_file.exists():
            if not bootstrap_if_missing:
                return {
                    "schema_version": LOCAL_BRAIN_REGISTRY_SCHEMA_VERSION,
                    "registry_id": "local",
                    "brain_root": str((brain_root or brain_root_path()).resolve()),
                    "brains": [],
                    "validation": validate_local_brain_registry({"schema_version": LOCAL_BRAIN_REGISTRY_SCHEMA_VERSION, "brains": []}),
                }
            return bootstrap_local_brain_registry(brain_root=brain_root)
        registry = _json_file(registry_file)
        registry["brain_count"] = len(list(registry.get("brains") or []))
        registry["validation"] = validate_local_brain_registry(registry)
        return registry


def set_active_brain(
    brain_id: str,
    *,
    make_default: bool = False,
    brain_root: Path | None = None,
) -> dict[str, Any]:
    target = str(brain_id or "").strip()
    registry = load_local_brain_registry(brain_root=brain_root)
    brains = [dict(item) for item in list(registry.get("brains") or []) if isinstance(item, dict)]
    if target not in {str(item.get("brain_id") or "") for item in brains}:
        raise BrainRegistryError(f"unknown_brain_id:{target}")
    for item in brains:
        is_target = str(item.get("brain_id") or "") == target
        item["is_active"] = is_target
        if make_default:
            item["is_default"] = is_target
        item["updated_at"] = utc_timestamp() if is_target else item.get("updated_at")
    registry["brains"] = brains
    registry["brain_count"] = len(brains)
    registry["active_brain_id"] = target
    if make_default:
        registry["default_brain_id"] = target
    registry["updated_at"] = utc_timestamp()
    registry["validation"] = validate_local_brain_registry(registry)
    _atomic_write_json(brain_registry_path(brain_root), registry)
    return registry


def resolve_brain_scope(
    brain_id: str | None = None,
    *,
    brain_root: Path | None = None,
    require_explicit: bool = False,
) -> dict[str, Any]:
    root = (brain_root or brain_root_path()).resolve()
    registry = load_local_brain_registry(brain_root=root)
    brains = [dict(item) for item in list(registry.get("brains") or []) if isinstance(item, dict)]
    by_id = {str(item.get("brain_id") or ""): item for item in brains}
    requested = str(brain_id or "").strip()
    if requested:
        if requested not in by_id:
            raise BrainRegistryError(f"unknown_brain_id:{requested}")
        resolved_record = by_id[requested]
        return _reconcile_resolved_brain_record(resolved_record, brain_root=root)
    if require_explicit:
        raise BrainRegistryError("brain_id_required")
    validation = dict(registry.get("validation") or {})
    default_id = str(registry.get("default_brain_id") or "")
    active_id = str(registry.get("active_brain_id") or "")
    resolved = active_id or default_id
    if not bool(validation.get("safe_default_configured")) or resolved not in by_id:
        raise BrainRegistryError("ambiguous_brain_scope_without_safe_default")
    return _reconcile_resolved_brain_record(by_id[resolved], brain_root=root)


def _reconcile_resolved_brain_record(
    record: dict[str, Any],
    *,
    brain_root: Path,
) -> dict[str, Any]:
    brain_id = str(record.get("brain_id") or "").strip()
    if not brain_id:
        raise BrainRegistryError("brain_id_required")
    storage = _portable_registered_storage_path(record, brain_root=brain_root, brain_id=brain_id)
    persisted_node_count = int(record.get("node_count") or 0)
    recorded_storage = Path(str(record.get("storage_path") or brain_root / brain_id / "storage")).expanduser()
    if storage == recorded_storage and _node_count(storage) == persisted_node_count:
        return record
    return refresh_local_brain_record(brain_id, brain_root=brain_root)


def active_brain_summary(*, brain_root: Path | None = None) -> dict[str, Any]:
    registry = load_local_brain_registry(brain_root=brain_root)
    active_id = str(registry.get("active_brain_id") or "")
    active = next((dict(item) for item in list(registry.get("brains") or []) if str((item or {}).get("brain_id") or "") == active_id), {})
    return {
        "schema_version": "agvm.local_active_brain_summary.v1",
        "brain_id": active.get("brain_id"),
        "display_name": active.get("display_name"),
        "storage_path": active.get("storage_path"),
        "storage_layout": active.get("storage_layout"),
        "safe_for_mcp": bool(active.get("safe_for_mcp")),
        "runtime_scope_status": registry.get("runtime_scope_status"),
        "registry_path": registry.get("registry_path"),
        "brain_count": len(list(registry.get("brains") or [])),
    }
