# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: Apache-2.0

"""Public AGVM SDK contract package.

The SDK contains dependency-light contracts shared by public core, private
modules and the future platform. Runtime product logic should stay outside this
package.
"""

from __future__ import annotations

__all__ = [
    "AGVM_SDK_SCHEMA_VERSION",
]

AGVM_SDK_SCHEMA_VERSION = "agvm.sdk.v1"
