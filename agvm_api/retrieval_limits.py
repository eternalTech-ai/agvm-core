# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations


DEFAULT_RETRIEVAL_MAX_MATCHES = 16
MAX_RETRIEVAL_MATCHES = 64
DEFAULT_RETRIEVAL_CANDIDATES_PER_STEP = 24
MAX_RETRIEVAL_CANDIDATES_PER_STEP = 128


def scaled_retrieval_candidate_limit(max_matches: int, *, overfetch_factor: int = 2) -> int:
    """Return a bounded candidate pool large enough to rerank the requested evidence."""

    requested_matches = max(1, min(MAX_RETRIEVAL_MATCHES, int(max_matches)))
    factor = max(1, int(overfetch_factor))
    return min(
        MAX_RETRIEVAL_CANDIDATES_PER_STEP,
        max(DEFAULT_RETRIEVAL_CANDIDATES_PER_STEP, requested_matches * factor),
    )
