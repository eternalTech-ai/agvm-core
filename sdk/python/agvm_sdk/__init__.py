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
