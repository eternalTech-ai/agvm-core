# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from brain_registry import brain_root_path

from .contracts import SESSION_SCHEMA_VERSION


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOCKS_GUARD = threading.Lock()
_SESSION_LOCKS: dict[str, threading.RLock] = {}


class BootstrapStoreError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class BootstrapSessionStore:
    def __init__(self, brain_record: dict[str, Any], *, brain_root: Path | None = None) -> None:
        raw_brain_path = str(brain_record.get("registry_brain_path") or "").strip()
        if not str(brain_record.get("brain_id") or "").strip() or not raw_brain_path:
            raise BootstrapStoreError("brain_scope_missing", status_code=404)
        root = (brain_root or brain_root_path()).expanduser().resolve()
        brain_path = Path(raw_brain_path).expanduser().resolve()
        try:
            brain_path.relative_to(root)
        except ValueError as exc:
            raise BootstrapStoreError("brain_scope_outside_brain_root", status_code=403) from exc
        self.brain_id = str(brain_record["brain_id"])
        self.root = brain_path / "brain_bootstrap_v1" / "sessions"

    @contextmanager
    def locked(self, session_id: str) -> Iterator[None]:
        path = str(self._session_dir(session_id))
        with _LOCKS_GUARD:
            lock = _SESSION_LOCKS.setdefault(path, threading.RLock())
        with lock:
            yield

    def load_history(self, session_id: str) -> list[dict[str, Any]]:
        revisions_dir = self._session_dir(session_id) / "revisions"
        if not revisions_dir.is_dir():
            raise BootstrapStoreError("bootstrap_session_not_found", status_code=404)
        history = [self._read_json(path) for path in sorted(revisions_dir.glob("*.json"))]
        self._validate_history(history, session_id=session_id)
        return history

    def load_latest(self, session_id: str) -> dict[str, Any]:
        history = self.load_history(session_id)
        if not history:
            raise BootstrapStoreError("bootstrap_session_empty", status_code=409)
        return history[-1]

    def append(self, snapshot: dict[str, Any], *, expected_revision: int) -> dict[str, Any]:
        session_id = self._safe_id(snapshot.get("session_id"), field="session_id")
        session_dir = self._session_dir(session_id)
        revisions_dir = session_dir / "revisions"
        revisions_dir.mkdir(parents=True, exist_ok=True)
        existing = [self._read_json(path) for path in sorted(revisions_dir.glob("*.json"))]
        self._validate_history(existing, session_id=session_id)
        current_revision = int(existing[-1]["revision"]) if existing else 0
        if current_revision != int(expected_revision):
            raise BootstrapStoreError(
                f"bootstrap_revision_conflict:expected={expected_revision}:actual={current_revision}",
                status_code=409,
            )

        record = dict(snapshot)
        record["schema_version"] = SESSION_SCHEMA_VERSION
        record["brain_id"] = self.brain_id
        record["session_id"] = session_id
        record["revision"] = current_revision + 1
        record["previous_revision_digest"] = existing[-1]["revision_digest"] if existing else None
        record.pop("revision_digest", None)
        record["revision_digest"] = _record_digest(record)
        path = revisions_dir / f"{record['revision']:08d}.json"
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(record, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise BootstrapStoreError("bootstrap_revision_conflict:file_already_exists", status_code=409) from exc
        return record

    def find_idempotent(
        self,
        history: list[dict[str, Any]],
        *,
        operation: str,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        matches = [
            revision
            for revision in history
            if dict(revision.get("request") or {}).get("operation") == operation
            and dict(revision.get("request") or {}).get("idempotency_key") == idempotency_key
        ]
        if not matches:
            return None
        if any(dict(item.get("request") or {}).get("request_digest") != request_digest for item in matches):
            raise BootstrapStoreError("bootstrap_idempotency_key_reused_with_different_request", status_code=409)
        return matches[-1]

    def _session_dir(self, session_id: str) -> Path:
        return self.root / self._safe_id(session_id, field="session_id")

    @staticmethod
    def _safe_id(value: Any, *, field: str) -> str:
        clean = str(value or "").strip()
        if not _SAFE_ID.fullmatch(clean):
            raise BootstrapStoreError(f"invalid_{field}", status_code=422)
        return clean

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BootstrapStoreError("bootstrap_revision_unreadable", status_code=409) from exc
        if not isinstance(value, dict):
            raise BootstrapStoreError("bootstrap_revision_not_object", status_code=409)
        return value

    @staticmethod
    def _validate_history(history: list[dict[str, Any]], *, session_id: str) -> None:
        previous_digest: str | None = None
        for index, revision in enumerate(history, start=1):
            if revision.get("schema_version") != SESSION_SCHEMA_VERSION:
                raise BootstrapStoreError("bootstrap_revision_schema_mismatch", status_code=409)
            if str(revision.get("session_id") or "") != session_id:
                raise BootstrapStoreError("bootstrap_revision_session_mismatch", status_code=409)
            if int(revision.get("revision") or 0) != index:
                raise BootstrapStoreError("bootstrap_revision_sequence_invalid", status_code=409)
            if revision.get("previous_revision_digest") != previous_digest:
                raise BootstrapStoreError("bootstrap_revision_chain_invalid", status_code=409)
            expected_digest = str(revision.get("revision_digest") or "")
            if expected_digest != _record_digest(revision):
                raise BootstrapStoreError("bootstrap_revision_digest_invalid", status_code=409)
            previous_digest = expected_digest


def request_digest(payload: dict[str, Any]) -> str:
    normalized = {key: value for key, value in payload.items() if key != "idempotency_key"}
    return hashlib.sha256(_canonical_bytes(normalized)).hexdigest()


def _record_digest(record: dict[str, Any]) -> str:
    normalized = {key: value for key, value in record.items() if key != "revision_digest"}
    return hashlib.sha256(_canonical_bytes(normalized)).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
