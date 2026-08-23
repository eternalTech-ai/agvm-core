# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

"""Backward-compatible adapter for the public AGVM SDK module release contract."""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_local_sdk_path() -> None:
    current_dir = Path(__file__).resolve().parent
    candidates = (
        current_dir / "sdk" / "python",
        current_dir.parent / "sdk" / "python",
    )
    for candidate in candidates:
        if (candidate / "agvm_sdk").is_dir():
            candidate_text = str(candidate)
            if candidate_text not in sys.path:
                sys.path.insert(0, candidate_text)
            return


_ensure_local_sdk_path()

from agvm_sdk.module_release import *  # noqa: F401,F403
