from __future__ import annotations

from collections import defaultdict
from typing import Any


def _truncate(text: str, limit: int = 220) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3].rstrip()}..."


def _node_priority(node: dict[str, Any]) -> tuple[float, float, float, str]:
    return (
        1.0 if node.get("is_document_anchor") else 0.0,
        float(node.get("memory_confidence") or node.get("derivation_confidence") or 0.0),
        float(node.get("stability_confidence") or 0.0),
        str(node.get("id") or ""),
    )


def _compact_node(node: dict[str, Any], visible_ids: set[str]) -> dict[str, Any]:
    payload = dict(node)
    payload["raw_text"] = _truncate(str(node.get("raw_text") or node.get("summary") or ""))
    payload["summary"] = _truncate(str(node.get("summary") or node.get("raw_text") or ""), limit=140)
    payload["debug"] = None
    payload["links"] = [
        dict(link)
        for link in list(node.get("links") or [])
        if str(link.get("target_node_id")) in visible_ids
    ][:8]
    payload["highways"] = [
        dict(link)
        for link in list(node.get("highways") or [])
        if str(link.get("target_node_id")) in visible_ids
    ][:8]
    return payload


def build_graph_view(graph: dict[str, Any], *, max_nodes: int = 1600) -> dict[str, Any]:
    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    total_nodes = len(nodes)
    total_edges = len(edges)
    if total_nodes <= max_nodes:
        selected_nodes = nodes
    else:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in nodes:
            bucket_key = str((node.get("bucket") or {}).get("key") or "unbucketed")
            buckets[bucket_key].append(node)
        for bucket_nodes in buckets.values():
            bucket_nodes.sort(key=_node_priority, reverse=True)

        selected_ids: list[str] = []
        bucket_lists = list(buckets.values())
        round_index = 0
        while len(selected_ids) < max_nodes:
            progressed = False
            for bucket_nodes in bucket_lists:
                if round_index < len(bucket_nodes):
                    node_id = str(bucket_nodes[round_index].get("id"))
                    if node_id not in selected_ids:
                        selected_ids.append(node_id)
                        progressed = True
                        if len(selected_ids) >= max_nodes:
                            break
            if not progressed:
                break
            round_index += 1
        selected_set = set(selected_ids)
        selected_nodes = [node for node in nodes if str(node.get("id")) in selected_set]

    visible_ids = {str(node.get("id")) for node in selected_nodes}
    compact_nodes = [_compact_node(node, visible_ids) for node in selected_nodes]
    compact_edges = [
        dict(edge)
        for edge in edges
        if str(edge.get("source_node_id")) in visible_ids and str(edge.get("target_node_id")) in visible_ids
    ]
    meta = dict(graph.get("meta") or {})
    meta.update(
        {
            "view_mode": "render",
            "sampled": total_nodes > len(compact_nodes),
            "total_node_count": total_nodes,
            "total_edge_count": total_edges,
            "sampled_node_count": len(compact_nodes),
            "sampled_edge_count": len(compact_edges),
        }
    )
    return {
        "version": graph.get("version"),
        "graph_name": graph.get("graph_name"),
        "nodes": compact_nodes,
        "edges": compact_edges,
        "meta": meta,
    }
