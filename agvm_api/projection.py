# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import math
import re
from typing import Any

from config import BUCKET_SIZE, FACET_FIELDS, GUIDE_AREAS, ROUTING_A, ROUTING_FIELDS


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def normalize_unit_confidence(value: Any, *, default: float | None = None) -> float | None:
    """Normalize confidence values while preserving legacy 0-10 compiler output.

    Confidence fields are stored and exposed on a 0-1 scale.  Some early AI
    compiler responses used the interview-style 0-10 scale, so a value such as
    ``9.0`` must become ``0.9`` rather than merely being clamped to ``1.0``.
    """

    if value is None or value == "":
        return default
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(normalized):
        return default
    if 1.0 < normalized <= 10.0:
        normalized /= 10.0
    return max(0.0, min(1.0, normalized))


def normalize_scores(values: dict[str, float], fields: list[str]) -> dict[str, float]:
    raw = {field: clamp01(values.get(field, 0.0)) for field in fields}
    total = sum(raw.values())
    if total <= 1e-12:
        uniform = 1.0 / len(fields)
        return {field: uniform for field in fields}
    return {field: round(raw[field] / total, 6) for field in fields}


def semantic_similarity(left: dict[str, float], right: dict[str, float], fields: list[str]) -> float:
    dot = sum(float(left.get(field, 0.0)) * float(right.get(field, 0.0)) for field in fields)
    left_mag = math.sqrt(sum(float(left.get(field, 0.0)) ** 2 for field in fields))
    right_mag = math.sqrt(sum(float(right.get(field, 0.0)) ** 2 for field in fields))
    if left_mag <= 1e-12 or right_mag <= 1e-12:
        return 0.0
    return max(0.0, min(1.0, dot / (left_mag * right_mag)))


def lexical_overlap(left: str, right: str) -> float:
    left_tokens = {token for token in re.findall(r"\w+", left.lower()) if len(token) > 2}
    right_tokens = {token for token in re.findall(r"\w+", right.lower()) if len(token) > 2}
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def distance(a: dict[str, float], b: dict[str, float]) -> float:
    return math.sqrt(
        (float(a["x"]) - float(b["x"])) ** 2
        + (float(a["y"]) - float(b["y"])) ** 2
        + (float(a["z"]) - float(b["z"])) ** 2
    )


def infer_guide_area(text: str) -> str | None:
    lowered = text.lower()
    if any(
        keyword in lowered
        for keyword in (
            "come mi chiamo",
            "come si chiama",
            "my name",
            "what is the name",
            "who am i",
            "chi sono",
            "chi e",
            "chi è",
            "mi chiamo",
            "sono ",
        )
    ):
        return "Identity"
    if any(keyword in lowered for keyword in ("partner", "fidanzat", "mentor", "mentore", "fratello", "sorella", "brother", "sister", "relationship")):
        return "Relationships"
    if any(keyword in lowered for keyword in ("come comunica", "come parla", "stile", "style", "tone", "voice", "communication", "diretto", "structured", "strutturato")):
        return "Expression"
    if any(keyword in lowered for keyword in ("valori", "principi", "values", "what matters", "cosa conta")):
        return "Values"
    if any(keyword in lowered for keyword in ("storia", "history", "passato", "biografia", "background", "timeline", "nel 20", "nel 19")):
        return "History"
    if any(keyword in lowered for keyword in ("documento", "document", "transcript", "interview", "review", "brief", "spec", "operativo")):
        return "Media Signals"
    for guide_area, keywords in GUIDE_AREAS.items():
        if any(keyword in lowered for keyword in keywords):
            return guide_area
    return None


def infer_memory_type(text: str, input_mode: str = "auto", node_kind_hint: str | None = None) -> str:
    lowered = text.lower()
    if input_mode == "document":
        return "document_anchor"
    if node_kind_hint:
        return node_kind_hint.strip().lower().replace(" ", "_")
    if re.search(r"\bi am\s+(?:building|constructing|working on|creating|developing)\b", lowered):
        return "project"
    if any(token in lowered for token in ("mi chiamo", "my name", "come si chiama", "chi sono", "chi e", "chi è", "i am", "my identity")):
        return "identity"
    if any(token in lowered for token in ("value", "values", "principle", "care deeply", "prefer", "matters", "valori", "principi", "cosa conta")):
        return "value"
    if any(token in lowered for token in ("style", "tone", "voice", "communication", "comunica", "parla in modo", "diretto", "strutturato")):
        return "identity_style"
    if any(
        token in lowered
        for token in (
            "project",
            "startup",
            "roadmap",
            "build",
            "product",
            "progetto",
            "platform",
            "initiative",
            "venture",
            "tool",
            "studio",
            "lab",
            "atlas",
            "orbit",
        )
    ):
        return "project"
    if any(token in lowered for token in ("code", "engineering", "architecture", "system", "api", "architett", "sistema")):
        return "technical"
    if any(token in lowered for token in ("workflow", "process", "step", "procedure", "execution", "operativo", "procedura")):
        return "operational"
    if any(token in lowered for token in ("family", "partner", "customer", "team", "collaborator", "mentor", "mentore", "fratello", "sorella", "relationship")):
        return "relational"
    if any(token in lowered for token in ("feel", "emotion", "fear", "love")):
        return "emotional"
    if re.search(r"\b(19|20)\d{2}\b", lowered) or any(token in lowered for token in ("sold", "founded", "acquired", "happened", "nel 20", "nel 19", "in passato", "ha iniziato", "ha lavorato")):
        return "episodic"
    if any(token in lowered for token in ("documento", "review", "transcript", "interview", "spec", "brief", "memo")):
        return "document_anchor"
    return "knowledge"


def summarize_text(text: str, limit: int = 88) -> str:
    summary = re.sub(r"\s+", " ", text).strip()
    if len(summary) > limit:
        summary = f"{summary[: limit - 3].rstrip()}..."
    return summary


def heuristic_projection(text: str, *, input_mode: str = "auto", node_kind_hint: str | None = None) -> dict[str, Any]:
    lowered = text.lower()
    scores = {field: 0.01 for field in ROUTING_FIELDS}
    facets = {field: 0.2 for field in FACET_FIELDS}

    keywords = {
        "self_core": (
            "i am",
            "identity",
            "myself",
            "sono",
            "my name",
            "mi chiamo",
            "come si chiama",
            "what is the name",
            "who am i",
            "chi sono",
            "name",
        ),
        "values": ("value", "principle", "care deeply", "vision", "purpose", "prefer", "matters"),
        "identity_style": ("style", "tone", "voice", "communication", "expression", "structured", "direct"),
        "projectual": ("project", "roadmap", "startup", "build", "product", "prototype"),
        "technical": ("code", "architecture", "engineering", "api", "system", "scalable"),
        "operational": ("workflow", "process", "step", "procedure", "execution", "plan"),
        "documental": ("document", "transcript", "spec", "file", "source", "readme", "interview"),
        "conceptual": ("concept", "framework", "definition", "theory", "model", "pattern"),
        "meta": ("memory", "rule", "governance", "meta", "system"),
        "relational": ("team", "partner", "family", "customer", "person", "collaborator"),
        "emotional": ("feel", "emotion", "fear", "love", "sensitive"),
        "episodic": ("happened", "when", "sold", "founded", "timeline", "acquired", "started"),
    }
    for field, words in keywords.items():
        hits = sum(1 for word in words if word in lowered)
        if hits:
            scores[field] += min(0.42, 0.12 * hits)

    if input_mode == "document":
        scores["documental"] += 0.35
        scores["conceptual"] += 0.10
        facets["source_reliability"] = 0.85
        facets["modality_bias"] = 0.85
    if re.search(r"\b(19|20)\d{2}\b", lowered):
        scores["episodic"] += 0.16
        facets["temporal_scope"] = 0.9
    if any(token in lowered for token in ("roadmap", "plan", "future", "long-term")):
        facets["planning_horizon"] = 0.88
    if any(token in lowered for token in ("framework", "definition", "model", "theory")):
        facets["abstraction_level"] = 0.82
    if any(token in lowered for token in ("i ", "my ", "we ")):
        facets["agency"] = 0.7
        facets["identity_centrality"] = 0.65
    if any(
        token in lowered
        for token in ("come mi chiamo", "come si chiama", "my name", "what is the name", "who am i", "chi sono", "mi chiamo")
    ):
        scores["self_core"] += 0.28
        facets["identity_centrality"] = 0.92
    if any(token in lowered for token in ("private", "family", "partner")):
        facets["intimacy"] = 0.82
    if any(token in lowered for token in ("company", "client", "customer", "team", "organization")):
        facets["institutional_vs_personal"] = 0.72
        facets["role_density"] = 0.7
    if any(token in lowered for token in ("always", "often", "recurring", "usually")):
        facets["recurrence_strength"] = 0.75

    node_kind = node_kind_hint or infer_memory_type(text, input_mode, node_kind_hint)
    summary = summarize_text(text)
    return {
        "summary": summary,
        "node_kind": str(node_kind).replace(" ", "_"),
        "memory_type": infer_memory_type(text, input_mode, node_kind_hint),
        "routing_semantic_scores": normalize_scores(scores, ROUTING_FIELDS),
        "routing_facets": normalize_scores(facets, FACET_FIELDS),
        "is_summary": False,
        "granularity": 0.32 if len(summary.split()) < 10 else 0.58,
        "novelty": 0.62,
        "document_anchor_recommendation": input_mode == "document",
        "expected_guide_area": infer_guide_area(text),
    }


def scores_to_latent_vector(scores: dict[str, float]) -> dict[str, float]:
    normalized = normalize_scores(scores, ROUTING_FIELDS)
    baseline = 1.0 / max(1, len(ROUTING_FIELDS))
    vector = [(normalized[field] - baseline) * 1.85 for field in ROUTING_FIELDS]
    projected = []
    for row in ROUTING_A:
        projected.append(round(sum(weight * score for weight, score in zip(row, vector)), 6))
    return {"x": projected[0], "y": projected[1], "z": projected[2]}


def compute_radius_value(
    scores: dict[str, float],
    facets: dict[str, float] | None = None,
    *,
    is_summary: bool = False,
    is_document_anchor: bool = False,
    granularity: float = 0.5,
    novelty: float = 0.5,
    radial_policy: dict[str, Any] | None = None,
) -> float:
    normalized = normalize_scores(scores, ROUTING_FIELDS)
    normalized_facets = normalize_scores(facets or {}, FACET_FIELDS)
    bands = {
        "core": (0.08, 0.24),
        "inner": (0.24, 0.42),
        "mid": (0.42, 0.68),
        "outer": (0.68, 0.94),
    }
    core_signal = (
        0.34 * normalized["self_core"]
        + 0.24 * normalized["values"]
        + 0.18 * normalized["meta"]
        + 0.16 * normalized_facets["identity_centrality"]
        + 0.08 * normalized_facets["source_reliability"]
    )
    inner_signal = (
        0.26 * normalized["projectual"]
        + 0.22 * normalized["technical"]
        + 0.20 * normalized["conceptual"]
        + 0.12 * normalized["operational"]
        + 0.10 * normalized_facets["planning_horizon"]
        + 0.10 * normalized_facets["abstraction_level"]
    )
    mid_signal = (
        0.26 * normalized["relational"]
        + 0.16 * normalized["identity_style"]
        + 0.16 * normalized["emotional"]
        + 0.16 * normalized["operational"]
        + 0.10 * normalized_facets["recurrence_strength"]
        + 0.08 * normalized_facets["expression_intensity"]
        + 0.08 * normalized_facets["agency"]
    )
    outer_signal = (
        0.34 * normalized["documental"]
        + 0.28 * normalized["episodic"]
        + 0.14 * normalized["operational"]
        + 0.12 * normalized_facets["temporal_scope"]
        + 0.12 * normalized_facets["modality_bias"]
    )
    if is_summary:
        core_signal += 0.06
        outer_signal -= 0.04
    if is_document_anchor:
        outer_signal += 0.22
    band_signals = {
        "core": max(0.0001, core_signal),
        "inner": max(0.0001, inner_signal),
        "mid": max(0.0001, mid_signal),
        "outer": max(0.0001, outer_signal),
    }
    total_signal = sum(band_signals.values())
    band_weights = {key: value / total_signal for key, value in band_signals.items()}
    natural_radius = 0.0
    for band_name, weight in band_weights.items():
        start, end = bands[band_name]
        band_bias = 0.50
        if band_name == "core":
            band_bias = 0.35 + 0.20 * clamp01(1.0 - novelty)
        elif band_name == "inner":
            band_bias = 0.42 + 0.24 * clamp01(granularity)
        elif band_name == "mid":
            band_bias = 0.50 + 0.20 * clamp01(normalized["relational"] + normalized["identity_style"])
        else:
            band_bias = 0.58 + 0.28 * clamp01(novelty + normalized["documental"] + normalized["episodic"])
        natural_radius += weight * (start + (end - start) * min(1.0, band_bias))
    if radial_policy:
        target_band = str(radial_policy.get("target_band") or "")
        band = bands.get(target_band)
        if band:
            bias = clamp01(float(radial_policy.get("band_bias") or 0.5))
            target_radius = band[0] + (band[1] - band[0]) * bias
            return max(0.05, min(0.96, 0.35 * natural_radius + 0.65 * target_radius))
    return max(0.05, min(0.96, natural_radius))


def latent_vector_to_angles(latent: dict[str, float]) -> dict[str, float]:
    x, y, z = float(latent["x"]), float(latent["y"]), float(latent["z"])
    norm = math.sqrt(x * x + y * y + z * z)
    if norm <= 1e-12:
        x, y, z = 0.0, 0.0, 1.0
        norm = 1.0
    ux, uy, uz = x / norm, y / norm, z / norm
    theta = math.atan2(uy, ux)
    if theta < 0:
        theta += 2.0 * math.pi
    phi = math.acos(max(-1.0, min(1.0, uz)))
    return {"theta": theta, "phi": phi}


def quantize_to_brainhex(theta: float, phi: float, radius_value: float) -> dict[str, int | str]:
    theta_bin = int(round((theta / (2.0 * math.pi)) * 255.0))
    phi_bin = int(round((phi / math.pi) * 255.0))
    radius_bin = int(round(max(0.0, min(1.0, radius_value)) * 255.0))
    theta_bin = max(0, min(255, theta_bin))
    phi_bin = max(0, min(255, phi_bin))
    radius_bin = max(0, min(255, radius_bin))
    return {
        "theta_bin": theta_bin,
        "phi_bin": phi_bin,
        "radius_bin": radius_bin,
        "code": f"#{theta_bin:02X}{phi_bin:02X}{radius_bin:02X}",
    }


def brainhex_to_position(brainhex: dict[str, int | str], r_min: float = 0.05, r_max: float = 1.0) -> dict[str, float]:
    theta = 2.0 * math.pi * (int(brainhex["theta_bin"]) / 255.0)
    phi = math.pi * (int(brainhex["phi_bin"]) / 255.0)
    radius = r_min + (r_max - r_min) * (int(brainhex["radius_bin"]) / 255.0)
    return {
        "x": float(radius * math.sin(phi) * math.cos(theta)),
        "y": float(radius * math.sin(phi) * math.sin(theta)),
        "z": float(radius * math.cos(phi)),
    }


def brainhex_to_hsl(brainhex: dict[str, int | str]) -> dict[str, float]:
    theta_ratio = int(brainhex["theta_bin"]) / 255.0
    phi_ratio = int(brainhex["phi_bin"]) / 255.0
    radius_ratio = int(brainhex["radius_bin"]) / 255.0
    h = 360.0 * theta_ratio
    s = 52.0 + 28.0 * (1.0 - abs(2.0 * phi_ratio - 1.0))
    l = 68.0 - 26.0 * radius_ratio
    return {"h": h, "s": max(22.0, min(92.0, s)), "l": max(24.0, min(78.0, l))}


def hsl_to_rgb(h: float, s: float, l: float) -> tuple[int, int, int]:
    h = (h % 360.0) / 360.0
    s = max(0.0, min(1.0, s / 100.0))
    l = max(0.0, min(1.0, l / 100.0))
    if s == 0:
        value = int(round(l * 255))
        return value, value, value

    def hue_to_rgb(p: float, q: float, t: float) -> float:
        if t < 0:
            t += 1
        if t > 1:
            t -= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    r = hue_to_rgb(p, q, h + 1 / 3)
    g = hue_to_rgb(p, q, h)
    b = hue_to_rgb(p, q, h - 1 / 3)
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


def color_from_brainhex(brainhex: dict[str, int | str]) -> dict[str, float | str]:
    hsl = brainhex_to_hsl(brainhex)
    r, g, b = hsl_to_rgb(hsl["h"], hsl["s"], hsl["l"])
    return {**hsl, "hex": f"#{r:02X}{g:02X}{b:02X}"}


def position_to_angles(position: dict[str, float]) -> dict[str, float]:
    x, y, z = float(position["x"]), float(position["y"]), float(position["z"])
    radius = math.sqrt(x * x + y * y + z * z)
    if radius <= 1e-12:
        return {"theta": 0.0, "phi": 0.0, "radius": 0.0}
    theta = math.atan2(y, x)
    if theta < 0:
        theta += 2.0 * math.pi
    phi = math.acos(max(-1.0, min(1.0, z / radius)))
    return {"theta": theta, "phi": phi, "radius": min(1.0, max(0.0, radius))}


def position_to_topology_brainhex(position: dict[str, float]) -> dict[str, int | str]:
    angles = position_to_angles(position)
    return quantize_to_brainhex(angles["theta"], angles["phi"], angles["radius"])


def position_to_bucket(position: dict[str, float], *, bucket_size: float = BUCKET_SIZE) -> dict[str, int | str]:
    bx = int(math.floor(float(position["x"]) / bucket_size))
    by = int(math.floor(float(position["y"]) / bucket_size))
    bz = int(math.floor(float(position["z"]) / bucket_size))
    return {"x": bx, "y": by, "z": bz, "key": f"{bx}:{by}:{bz}"}
