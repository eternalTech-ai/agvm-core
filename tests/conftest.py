# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import faulthandler
import sys
from pathlib import Path

import pytest


LEGACY_ROOT_DOC_MARKERS = (
    "AGVM_MCP_First_Human_Memory_Master_v9_2026-05-07.md",
    "AGVM_Product_Ready_Roadmap_2026-05-06.md",
    "AGVM_Canonical_Document_Index_2026-05-07.md",
    "AGVM_PR12H_to_PR12L_Product_Ready_Architecture_Spec_2026-05-07.md",
    "AGVM_PR12P_14R_EternalTech_Brain_OS_V3_Run_Projection_And_Local_MCP_Readiness_Replan_2026-05-12.md",
)

LEGACY_DOC_TEST_NAME_MARKERS = (
    "doc",
    "docs",
    "document",
    "documentation",
    "report",
    "roadmap",
    "canonical",
    "replan",
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip obsolete root-report assertions after the active docs reset."""
    cache: dict[Path, str] = {}
    skip_legacy_doc_test = pytest.mark.skip(
        reason="Legacy root AGVM report docs were retired; active docs live under docs/AGVM_*.md."
    )
    for item in items:
        path = Path(str(item.fspath))
        if path.suffix != ".py":
            continue
        text = cache.get(path)
        if text is None:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            cache[path] = text
        if not any(marker in text for marker in LEGACY_ROOT_DOC_MARKERS):
            continue
        name = str(item.name).lower()
        if any(marker in name for marker in LEGACY_DOC_TEST_NAME_MARKERS):
            item.add_marker(skip_legacy_doc_test)


@pytest.fixture
def windows_threaded_asgi_faulthandler_guard(request: pytest.FixtureRequest):
    """Suppress handled first-chance Windows SEH noise around threaded TestClient calls.

    Pytest's faulthandler plugin installs a Windows exception handler that can
    print "Windows fatal exception: access violation" for handled first-chance
    access violations raised underneath AnyIO/Starlette's blocking portal.  The
    affected in-process ASGI tests complete with exit code 0; keep the guard
    scoped to those tests so ordinary faulthandler coverage remains available
    elsewhere.
    """

    was_enabled = faulthandler.is_enabled()
    restore_fd: int | None = None
    try:
        from _pytest.faulthandler import fault_handler_stderr_fd_key

        if fault_handler_stderr_fd_key in request.config.stash:
            restore_fd = request.config.stash[fault_handler_stderr_fd_key]
    except Exception:
        restore_fd = None
    if sys.platform == "win32" and was_enabled:
        faulthandler.disable()
    try:
        yield
    finally:
        if sys.platform == "win32" and was_enabled:
            if restore_fd is not None:
                faulthandler.enable(file=restore_fd)
            else:
                faulthandler.enable()
