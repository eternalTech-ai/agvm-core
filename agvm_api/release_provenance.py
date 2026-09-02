# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import os
from typing import Any

from agvm_sdk.release_bundle import runtime_release_bundle_status


RELEASE_PROVENANCE_SCHEMA_VERSION = "detwin.runtime.release.v1"


def runtime_release_provenance(*, component: str, default_version: str) -> dict[str, Any]:
    """Return immutable image provenance injected by the release build."""

    bundle_status = runtime_release_bundle_status(component=component)
    return {
        "schema_version": RELEASE_PROVENANCE_SCHEMA_VERSION,
        "component": component,
        "version": _env_value("AGVM_IMAGE_VERSION", default_version),
        "revision": bundle_status["image_revision"] or "unknown",
        "source_sha": bundle_status["source_sha"] or "unknown",
        "release_bundle": bundle_status["bundle_id"] or "unknown",
        "release_bundle_status": bundle_status,
    }


def _env_value(name: str, fallback: str) -> str:
    value = str(os.getenv(name, "")).strip()
    return value or fallback
