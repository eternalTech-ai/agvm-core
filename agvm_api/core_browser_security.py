# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_LOOPBACK_ORIGIN_REGEX = r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?"


def install_core_browser_security(app: FastAPI) -> None:
    trusted_origins = _configured_trusted_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=trusted_origins,
        allow_origin_regex=_LOOPBACK_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def enforce_browser_origin(request: Request, call_next):  # type: ignore[no-untyped-def]
        origin = str(request.headers.get("origin") or "").strip()
        if origin and not core_browser_origin_allowed(origin, trusted_origins=trusted_origins):
            return JSONResponse(
                status_code=403,
                content={"detail": "agvm_core_browser_origin_forbidden"},
            )
        return await call_next(request)


def core_browser_origin_allowed(
    origin: str,
    *,
    trusted_origins: tuple[str, ...] | None = None,
) -> bool:
    clean = str(origin or "").strip().rstrip("/")
    if not clean or clean == "null":
        return False
    try:
        parsed = urlsplit(clean)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        return False
    if parsed.hostname.lower() in _LOOPBACK_HOSTS:
        return True
    return clean in (trusted_origins if trusted_origins is not None else _configured_trusted_origins())


def _configured_trusted_origins() -> tuple[str, ...]:
    values: list[str] = []
    for raw in str(os.getenv("AGVM_CORE_TRUSTED_BROWSER_ORIGINS") or "").split(","):
        origin = raw.strip().rstrip("/")
        if not origin or origin in values:
            continue
        try:
            parsed = urlsplit(origin)
        except ValueError:
            continue
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        ):
            values.append(origin)
    return tuple(values)
