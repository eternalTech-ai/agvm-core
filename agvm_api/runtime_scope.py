from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from config import DATA_DIR


SQLITE_FILENAME = "beta_vector_memory.sqlite3"
GRAPH_FILENAME = "beta_vector_memory.graph.json"
GRAPH_VIEW_FILENAME = "beta_vector_memory.graph.view.json"
INDEX_FILENAME = "beta_vector_memory.index.json"
ATLAS_FILENAME = "beta_vector_memory.atlas.json"


_CURRENT_BRAIN_ID: ContextVar[str | None] = ContextVar("agvm_current_brain_id", default=None)
_CURRENT_STORAGE_PATH: ContextVar[str | None] = ContextVar("agvm_current_storage_path", default=None)
_CURRENT_BRAIN_RECORD: ContextVar[dict[str, Any] | None] = ContextVar("agvm_current_brain_record", default=None)


def current_brain_id() -> str | None:
    brain_id = _CURRENT_BRAIN_ID.get()
    return str(brain_id).strip() or None if brain_id is not None else None


def current_brain_record() -> dict[str, Any]:
    record = _CURRENT_BRAIN_RECORD.get()
    return dict(record or {})


def current_data_dir() -> Path:
    scoped_path = _CURRENT_STORAGE_PATH.get()
    if scoped_path:
        return Path(scoped_path).expanduser().resolve()
    return DATA_DIR.resolve()


def current_sqlite_path() -> Path:
    return current_data_dir() / SQLITE_FILENAME


def current_graph_path() -> Path:
    return current_data_dir() / GRAPH_FILENAME


def current_graph_view_path() -> Path:
    return current_data_dir() / GRAPH_VIEW_FILENAME


def current_index_path() -> Path:
    return current_data_dir() / INDEX_FILENAME


def current_atlas_path() -> Path:
    return current_data_dir() / ATLAS_FILENAME


def current_runtime_paths() -> dict[str, str]:
    return {
        "data_dir": str(current_data_dir()),
        "sqlite": str(current_sqlite_path()),
        "graph": str(current_graph_path()),
        "graph_view": str(current_graph_view_path()),
        "index": str(current_index_path()),
        "atlas": str(current_atlas_path()),
    }


def runtime_scope_summary() -> dict[str, Any]:
    return {
        "schema_version": "agvm.runtime_scope.v1",
        "brain_id": current_brain_id(),
        "brain": current_brain_record(),
        "paths": current_runtime_paths(),
    }


@contextmanager
def use_runtime_brain(record: dict[str, Any]) -> Iterator[dict[str, Any]]:
    brain = dict(record or {})
    brain_id = str(brain.get("brain_id") or "").strip()
    storage_path = str(brain.get("storage_path") or "").strip()
    if not brain_id:
        raise ValueError("brain_id_required")
    if not storage_path:
        raise ValueError(f"storage_path_required_for_brain:{brain_id}")

    brain_token = _CURRENT_BRAIN_ID.set(brain_id)
    path_token = _CURRENT_STORAGE_PATH.set(str(Path(storage_path).expanduser().resolve()))
    record_token = _CURRENT_BRAIN_RECORD.set(brain)
    try:
        yield brain
    finally:
        _CURRENT_BRAIN_RECORD.reset(record_token)
        _CURRENT_STORAGE_PATH.reset(path_token)
        _CURRENT_BRAIN_ID.reset(brain_token)
