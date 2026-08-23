# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

"""Public retrieval fallback when the paid Brain Profile runtime is absent."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


PUBLIC_CLOUD_ACTION_STUB = True


def rerank_selected_top_k_with_brain_profile(
    probe: Mapping[str, Any],
    baseline_matches: list[dict[str, Any]],
    *,
    top_k: int,
    data_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Preserve the authoritative baseline; Public Core has no paid reranker."""

    _ = probe, data_dir
    return list(baseline_matches[: max(0, int(top_k))])
