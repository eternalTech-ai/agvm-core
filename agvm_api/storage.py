from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import ATLAS_VERSION, BUCKET_SIZE, GRAPH_VERSION, INDEX_VERSION
from runtime_scope import current_atlas_path, current_data_dir, current_graph_path, current_graph_view_path, current_index_path


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_data_dir() -> None:
    current_data_dir().mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_data_dir()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        os.close(fd)
        Path(tmp_name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        Path(tmp_name).replace(path)
    finally:
        tmp_path = Path(tmp_name)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def empty_graph() -> dict[str, Any]:
    return {
        "version": GRAPH_VERSION,
        "graph_name": "AGVM Lab",
        "nodes": [],
        "edges": [],
        "meta": {
            "created_from": "agvm_lab_local_only",
            "graph_updated_at": utc_timestamp(),
        },
    }


def empty_graph_view() -> dict[str, Any]:
    payload = empty_graph()
    payload["meta"] = {
        **dict(payload.get("meta") or {}),
        "view_mode": "render",
        "sampled": False,
        "total_node_count": 0,
        "total_edge_count": 0,
        "sampled_node_count": 0,
        "sampled_edge_count": 0,
    }
    return payload


def empty_index() -> dict[str, Any]:
    return {
        "version": INDEX_VERSION,
        "bucket_size": BUCKET_SIZE,
        "spatial_index": {},
        "bucket_index": {},
        "highway_index": {},
        "document_index": [],
        "node_position_map": {},
        "updated_at": utc_timestamp(),
    }


def empty_atlas() -> dict[str, Any]:
    return {
        "version": ATLAS_VERSION,
        "bucket_size": BUCKET_SIZE,
        "generated_at": utc_timestamp(),
        "node_count": 0,
        "bucket_count": 0,
        "buckets": [],
    }


def load_json_or_default(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    ensure_data_dir()
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def load_graph() -> dict[str, Any]:
    return load_json_or_default(current_graph_path(), empty_graph())


def save_graph(graph: dict[str, Any]) -> dict[str, Any]:
    payload = {**empty_graph(), **dict(graph or {})}
    payload["version"] = GRAPH_VERSION
    payload["nodes"] = list(payload.get("nodes") or [])
    payload["edges"] = list(payload.get("edges") or [])
    payload["meta"] = {**dict(payload.get("meta") or {}), "graph_updated_at": utc_timestamp()}
    atomic_write_json(current_graph_path(), payload)
    return payload


def load_graph_view() -> dict[str, Any]:
    return load_json_or_default(current_graph_view_path(), empty_graph_view())


def save_graph_view(graph_view: dict[str, Any]) -> dict[str, Any]:
    payload = {**empty_graph_view(), **dict(graph_view or {})}
    payload["nodes"] = list(payload.get("nodes") or [])
    payload["edges"] = list(payload.get("edges") or [])
    payload["meta"] = {**dict(payload.get("meta") or {}), "graph_updated_at": utc_timestamp()}
    atomic_write_json(current_graph_view_path(), payload)
    return payload


def load_index() -> dict[str, Any]:
    return load_json_or_default(current_index_path(), empty_index())


def save_index(index_payload: dict[str, Any]) -> dict[str, Any]:
    payload = {**empty_index(), **dict(index_payload or {}), "updated_at": utc_timestamp()}
    atomic_write_json(current_index_path(), payload)
    return payload


def load_atlas() -> dict[str, Any]:
    return load_json_or_default(current_atlas_path(), empty_atlas())


def save_atlas(atlas_payload: dict[str, Any]) -> dict[str, Any]:
    payload = {**empty_atlas(), **dict(atlas_payload or {}), "generated_at": utc_timestamp()}
    atomic_write_json(current_atlas_path(), payload)
    return payload


def bootstrap_files() -> None:
    ensure_data_dir()
    if not current_graph_path().exists():
        save_graph(empty_graph())
    if not current_graph_view_path().exists():
        save_graph_view(empty_graph_view())
    if not current_index_path().exists():
        save_index(empty_index())
    if not current_atlas_path().exists():
        save_atlas(empty_atlas())


def reset_memory_files() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    graph = save_graph(empty_graph())
    save_graph_view(empty_graph_view())
    index_payload = save_index(empty_index())
    atlas = save_atlas(empty_atlas())
    return graph, index_payload, atlas
