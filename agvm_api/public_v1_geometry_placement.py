# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Any


def apply_public_v1_geometry_profile_to_seed(
    seed: dict[str, Any],
    geometry_profile_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep the deterministic V1 seed unchanged unless a public profile exists.

    BrainProfileV1 reranks an already generated candidate pool and owns a
    separate display projection. It never rewrites operational coordinates.
    """

    _ = geometry_profile_context
    return seed
