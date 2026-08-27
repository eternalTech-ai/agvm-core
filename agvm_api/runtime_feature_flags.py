# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import os
from collections.abc import Mapping


GROW_V2_FLAG = "AGVM_GROW_V2_ENABLED"
MAINTAIN_INVESTIGATOR_FLAG = "AGVM_MAINTAIN_INVESTIGATOR_ENABLED"
CALIBRATE_V2_FLAG = "AGVM_CALIBRATE_V2_ENABLED"

_TRUE_VALUES = frozenset({"1", "true", "on"})
_FALSE_VALUES = frozenset({"0", "false", "off"})


class RuntimeFeatureFlagError(RuntimeError):
    """Stable fail-closed error for a disabled or malformed runtime flag."""

    def __init__(self, code: str, *, flag_name: str, configured_value: str | None) -> None:
        super().__init__(code)
        self.code = code
        self.flag_name = flag_name
        self.configured_value = configured_value


def runtime_feature_enabled(
    flag_name: str,
    *,
    default_enabled: bool = True,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Resolve one runtime flag with an explicit context-owned absence policy."""

    source = os.environ if environ is None else environ
    raw_value = source.get(flag_name)
    if raw_value is None:
        return bool(default_enabled)
    normalized = str(raw_value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RuntimeFeatureFlagError(
        "runtime_feature_flag_invalid",
        flag_name=flag_name,
        configured_value=str(raw_value),
    )


def require_runtime_feature(
    flag_name: str,
    *,
    disabled_code: str,
    default_enabled: bool = True,
    environ: Mapping[str, str] | None = None,
) -> None:
    if runtime_feature_enabled(
        flag_name,
        default_enabled=default_enabled,
        environ=environ,
    ):
        return
    source = os.environ if environ is None else environ
    raise RuntimeFeatureFlagError(
        disabled_code,
        flag_name=flag_name,
        configured_value=source.get(flag_name),
    )


__all__ = [
    "CALIBRATE_V2_FLAG",
    "GROW_V2_FLAG",
    "MAINTAIN_INVESTIGATOR_FLAG",
    "RuntimeFeatureFlagError",
    "require_runtime_feature",
    "runtime_feature_enabled",
]
