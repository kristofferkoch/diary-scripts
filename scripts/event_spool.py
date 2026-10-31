"""Deterministic, immutable events in a pipeline-readable trusted spool.

Collectors construct events from freshly fetched in-memory records. The spool is
not an ingest boundary for the legacy group-writable JSONL files. Event IDs are
stable across processes and restarts, and final files are linked into place
atomically without replacing an existing event.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Iterator

from scripts.safe_io import open_dir

SCHEMA_VERSION = 1
SOURCES = frozenset({"mail", "spond", "vigilo", "skolemelding"})
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_TEXT = MAX_EVENT_BYTES
EVENT_ID_RE = re.compile(r"evt_[0-9a-f]{64}\Z")
KIND_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
REVISION_RE = re.compile(r"(?:notmuch-v1|sha256:[0-9a-f]{64})\Z")
EVENT_FIELDS = frozenset({
    "schema_version", "event_id", "source", "kind", "native_id", "revision", "payload",
})


class EventValidationError(ValueError):
    """An event or serialized spool entry does not match the strict schema."""


class EventConflictError(RuntimeError):
    """An immutable event ID already names different content."""


def _validate_json(value: Any, path: str = "event", depth: int = 0) -> None:
    if depth > 64:
        raise EventValidationError(f"{path} exceeds the JSON nesting limit")
    if value is None or isinstance(value, (bool, str, int)):
        if isinstance(value, str) and len(value) > MAX_TEXT:
            raise EventValidationError(f"{path} string is too long")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EventValidationError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]", depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventValidationError(f"{path} has a non-string object key")
            _validate_json(item, f"{path}.{key}", depth + 1)
        return
    raise EventValidationError(f"{path} contains a non-JSON value")


def canonical_json(value: Any) -> bytes:
    _validate_json(value)
    try:
        text = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise EventValidationError(str(exc)) from exc
    return text.encode("utf-8")


def payload_revision(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


def event_id_for(source: str, kind: str, native_id: str, revision: str) -> str:
    identity = canonical_json({
        "source": source, "kind": kind, "native_id": native_id, "revision": revision,
    })
    return "evt_" + hashlib.sha256(identity).hexdigest()


def build_event(
    source: str,
    kind: str,
    native_id: str,
    payload: dict[str, Any],
    *,
    revision: str | None = None,
) -> dict[str, Any]:
    revision = revision or payload_revision(payload)
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id_for(source, kind, native_id, revision),
        "source": source,
        "kind": kind,
        "native_id": native_id,
        "revision": revision,
        "payload": payload,
    }
    validate_event(event)
    return event


def validate_event(event: Any) -> None:
    if not isinstance(event, dict):
        raise EventValidationError("event must be an object")
    if set(event) != EVENT_FIELDS:
        raise EventValidationError("event fields do not match the schema")
    if type(event["schema_version"]) is not int or event["schema_version"] != SCHEMA_VERSION:
        raise EventValidationError("unsupported schema_version")
    if not isinstance(event["source"], str) or event["source"] not in SOURCES:
        raise EventValidationError("unknown source")
    if not isinstance(event["kind"], str) or not KIND_RE.fullmatch(event["kind"]):
        raise EventValidationError("invalid kind")
    if not isinstance(event["native_id"], str) or not event["native_id"] \
            or len(event["native_id"]) > 4096 or "\x00" in event["native_id"]:
        raise EventValidationError("invalid native_id")
    if not isinstance(event["revision"], str) or not REVISION_RE.fullmatch(event["revision"]):
        raise EventValidationError("invalid revision")
    if not isinstance(event["payload"], dict):
        raise EventValidationError("payload must be an object")
    _validate_json(event["payload"], "payload")
    if event["revision"] == "notmuch-v1":
        if event["source"] != "mail" or event["kind"] != "message":
            raise EventValidationError("notmuch-v1 is only valid for mail messages")
    elif event["revision"] != payload_revision(event["payload"]):
        raise EventValidationError("revision does not match payload")
    expected = event_id_for(
        event["source"], event["kind"], event["native_id"], event["revision"],
    )
    if event["event_id"] != expected:
        raise EventValidationError("event_id does not match immutable identity")


def _reject_constant(value: str) -> None:
    raise EventValidationError(f"invalid JSON constant {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EventValidationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def parse_event(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_EVENT_BYTES:
        raise EventValidationError("serialized event is too large")
    try:
        text = data.decode("utf-8", "strict")
        event = json.loads(
            text, parse_constant=_reject_constant, object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EventValidationError("malformed event JSON") from exc
    validate_event(event)
    return event


class EventSpool:
    """A flat directory of immutable ``<event-id>.json`` files."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @staticmethod
    def _filename(event_id: str) -> str:
        if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
            raise EventValidationError("invalid event_id")
        return event_id + ".json"

    def _open_root(self) -> int:
        fd = open_dir(self.root)
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                raise EventValidationError("spool root is not a directory")
            if info.st_mode & 0o027:
                raise EventValidationError("spool root must not be group/other accessible")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def path_for(self, event_id: str) -> Path:
        return self.root / self._filename(event_id)

    def _read_at(self, dir_fd: int, event_id: str) -> dict[str, Any]:
        name = self._filename(event_id)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise EventValidationError("spool entry must be a single-link regular file")
            if info.st_size > MAX_EVENT_BYTES:
                raise EventValidationError("serialized event is too large")
            chunks: list[bytes] = []
            remaining = MAX_EVENT_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 128 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            event = parse_event(b"".join(chunks))
            if event["event_id"] != event_id:
                raise EventValidationError("filename does not match event_id")
            return event
        finally:
            os.close(fd)

    def read(self, event_id: str) -> dict[str, Any]:
        dir_fd = self._open_root()
        try:
            return self._read_at(dir_fd, event_id)
        finally:
            os.close(dir_fd)

    def contains(self, event_id: str) -> bool:
        dir_fd = self._open_root()
        try:
            try:
                self._read_at(dir_fd, event_id)
            except FileNotFoundError:
                return False
            return True
        finally:
            os.close(dir_fd)

    def write(self, event: dict[str, Any]) -> str:
        validate_event(event)
        encoded = canonical_json(event) + b"\n"
        if len(encoded) > MAX_EVENT_BYTES:
            raise EventValidationError("serialized event is too large")
        event_id = event["event_id"]
        name = self._filename(event_id)
        dir_fd = self._open_root()
        temp_name: str | None = None
        try:
            try:
                existing = self._read_at(dir_fd, event_id)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if existing != event:
                    raise EventConflictError(f"immutable event conflict: {event_id}")
                return "duplicate"

            temp_name = f".{event_id}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o640,
                dir_fd=dir_fd,
            )
            try:
                with os.fdopen(fd, "wb", closefd=False) as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(fd)
                os.fchmod(fd, 0o640)
            finally:
                os.close(fd)
            try:
                os.link(
                    temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                existing = self._read_at(dir_fd, event_id)
                if existing != event:
                    raise EventConflictError(f"immutable event conflict: {event_id}")
                return "duplicate"
            os.fsync(dir_fd)
            return "written"
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
            os.close(dir_fd)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        dir_fd = self._open_root()
        try:
            names = sorted(
                name for name in os.listdir(dir_fd)
                if name.endswith(".json") and EVENT_ID_RE.fullmatch(name[:-5])
            )
            for name in names:
                yield self._read_at(dir_fd, name[:-5])
        finally:
            os.close(dir_fd)
