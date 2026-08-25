# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


SOURCE_UPLOAD_MAX_BYTES = 25_000_000
SOURCE_RESPONSE_CHUNK_BYTES = 64 * 1024
SOURCE_REDIRECT_LIMIT = 5

_BLOCKED_SOURCE_HOSTS = {
    "instance-data",
    "instance-data.ec2.internal",
    "metadata",
    "metadata.azure.internal",
    "metadata.google.internal",
}
_BLOCKED_SOURCE_HOST_SUFFIXES = (".internal", ".local", ".localhost")


class SourceIntakeSecurityError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def sanitize_source_uri_for_persistence(value: Any) -> str | None:
    """Keep public source identity without persisting URL credentials or query secrets."""

    raw = str(value or "").strip()
    if not raw or len(raw) > 16_384 or any(ord(character) < 32 for character in raw):
        return None
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    host = str(parsed.hostname or "").rstrip(".").lower()
    if scheme not in {"http", "https"} or not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default_port = 443 if scheme == "https" else 80
    netloc = rendered_host if port in {None, default_port} else f"{rendered_host}:{port}"
    return urlunparse((scheme, netloc, parsed.path or "", "", "", ""))


def _public_ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value)
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if not address.is_global:
        raise SourceIntakeSecurityError("source_url_non_public_address")
    return address


def validate_public_source_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or any(ord(character) < 32 for character in raw) or "\\" in raw:
        raise SourceIntakeSecurityError("source_url_invalid")
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise SourceIntakeSecurityError("source_url_scheme_not_allowed")
    if parsed.username is not None or parsed.password is not None:
        raise SourceIntakeSecurityError("source_url_credentials_not_allowed")
    host = str(parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise SourceIntakeSecurityError("source_url_host_required")
    if (
        host in _BLOCKED_SOURCE_HOSTS
        or host == "localhost"
        or host.endswith(_BLOCKED_SOURCE_HOST_SUFFIXES)
    ):
        raise SourceIntakeSecurityError("source_url_host_not_allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SourceIntakeSecurityError("source_url_port_invalid") from exc
    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(
                host,
                port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise SourceIntakeSecurityError("source_url_host_unresolvable") from exc
        addresses = {str(item[4][0]).split("%", 1)[0] for item in resolved if item[4]}
        if not addresses:
            raise SourceIntakeSecurityError("source_url_host_unresolvable")
        for address in addresses:
            _public_ip_address(address)
    else:
        _public_ip_address(str(literal_address))
    return urlunparse(parsed._replace(fragment=""))


class PublicSourceRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        redirect_count = int(getattr(req, "_agvm_redirect_count", 0)) + 1
        if redirect_count > SOURCE_REDIRECT_LIMIT:
            raise SourceIntakeSecurityError("source_url_redirect_limit_exceeded")
        validated_url = validate_public_source_url(urljoin(req.full_url, newurl))
        redirected = super().redirect_request(req, fp, code, msg, headers, validated_url)
        if redirected is not None:
            setattr(redirected, "_agvm_redirect_count", redirect_count)
        return redirected


def open_public_source_request(request: Request, *, timeout_seconds: float) -> Any:
    validate_public_source_url(request.full_url)
    response = build_opener(PublicSourceRedirectHandler()).open(
        request,
        timeout=max(0.1, float(timeout_seconds)),
    )
    try:
        validate_public_source_url(str(response.geturl() or request.full_url))
    except Exception:
        response.close()
        raise
    return response


def read_response_bounded(
    response: Any,
    *,
    max_bytes: int,
    chunk_bytes: int = SOURCE_RESPONSE_CHUNK_BYTES,
) -> tuple[bytes, bool]:
    limit = max(1, int(max_bytes))
    chunk_limit = max(1, min(int(chunk_bytes), limit + 1))
    body = bytearray()
    while len(body) <= limit:
        remaining = limit + 1 - len(body)
        chunk = response.read(min(chunk_limit, remaining))
        if not chunk:
            break
        body.extend(chunk)
    truncated = len(body) > limit
    if truncated:
        del body[limit:]
    return bytes(body), truncated


async def read_upload_file_bounded(
    upload: Any,
    *,
    max_bytes: int = SOURCE_UPLOAD_MAX_BYTES,
    chunk_bytes: int = SOURCE_RESPONSE_CHUNK_BYTES,
) -> bytes:
    limit = max(1, int(max_bytes))
    declared_size = getattr(upload, "size", None)
    if isinstance(declared_size, int) and declared_size > limit:
        raise SourceIntakeSecurityError("source_upload_too_large")
    body = bytearray()
    while True:
        remaining = limit + 1 - len(body)
        if remaining <= 0:
            raise SourceIntakeSecurityError("source_upload_too_large")
        chunk = await upload.read(min(max(1, int(chunk_bytes)), remaining))
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > limit:
            raise SourceIntakeSecurityError("source_upload_too_large")
    return bytes(body)
