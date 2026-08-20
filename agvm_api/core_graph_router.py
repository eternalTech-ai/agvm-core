from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from brain_registry import BrainRegistryError, resolve_brain_scope
from runtime_scope import use_runtime_brain
from schemas import Graph, GraphResponse
from sqlite_store import bootstrap_runtime_store, fetch_atlas, fetch_graph_view


def create_core_graph_router() -> APIRouter:
    router = APIRouter()

    @router.get("/graph-view", response_model=GraphResponse)
    def get_graph_view(
        brain_id: str | None = Query(default=None),
        max_nodes: int = Query(default=1600, ge=100, le=5000),
        refresh: bool = False,
        memory_type: str | None = Query(default=None),
        guide_area: str | None = Query(default=None),
        confidence_floor: float | None = Query(default=None, ge=0.0, le=1.0),
        min_bx: int | None = Query(default=None),
        max_bx: int | None = Query(default=None),
        min_by: int | None = Query(default=None),
        max_by: int | None = Query(default=None),
        min_bz: int | None = Query(default=None),
        max_bz: int | None = Query(default=None),
    ) -> GraphResponse:
        del refresh
        brain_record = _resolve_brain_record(brain_id)
        bucket_window = None
        if None not in {min_bx, max_bx, min_by, max_by, min_bz, max_bz}:
            bucket_window = {
                "min_bx": int(min_bx),
                "max_bx": int(max_bx),
                "min_by": int(min_by),
                "max_by": int(max_by),
                "min_bz": int(min_bz),
                "max_bz": int(max_bz),
            }
        with use_runtime_brain(brain_record):
            bootstrap_runtime_store()
            graph_view = fetch_graph_view(
                max_nodes=max_nodes,
                memory_type=memory_type,
                guide_area=guide_area,
                bucket_window=bucket_window,
                confidence_floor=confidence_floor,
            )
            if not graph_view.get("nodes") and int((fetch_atlas().get("node_count") or 0)) > 0:
                graph_view = fetch_graph_view(max_nodes=max_nodes)
        return GraphResponse(graph=Graph(**graph_view))

    return router


def _resolve_brain_record(brain_id: str | None = None) -> dict[str, Any]:
    try:
        return resolve_brain_scope(brain_id=str(brain_id or "").strip() or None)
    except BrainRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
