"""Memory record model and storage adapters.

This module defines the minimal data model the rest of ``memory_hygiene``
operates on, plus an abstract :class:`MemoryStore` interface so that the audit
and maintenance passes can run against any backend.

Two working reference backends are included: :class:`JsonMemoryStore` (a single
JSON file) and :class:`SqliteMemoryStore` (a single SQLite file). Both are
standard library only.
"""

from __future__ import annotations

import abc
import json
import os
import sqlite3
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional

__all__ = [
    "MemoryRecord",
    "MemoryStore",
    "JsonMemoryStore",
    "SqliteMemoryStore",
    "parse_timestamp",
    "format_timestamp",
    "utc_now",
]


def utc_now() -> datetime:
    """Return the current moment as a timezone-aware UTC datetime.

    This is the default clock for every pass. ``datetime.utcnow()`` is not
    used: it returns a *naive* datetime that merely happens to hold UTC wall
    time, which silently compares wrong against anything aware, and it is
    deprecated from Python 3.12 onwards.
    """
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Coerce a datetime to timezone-aware UTC for comparison purposes.

    The coercion rule, applied everywhere two datetimes meet:

    * ``None`` passes through unchanged.
    * A **naive** datetime is assumed to already be expressed in UTC, so it is
      simply tagged with ``timezone.utc``. No wall-clock arithmetic happens.
      This is the only safe reading: every writer in this codebase that ever
      produced naive timestamps produced them from a UTC clock, and guessing
      a local zone instead would shift stored history by the machine's offset.
    * An **aware** datetime is converted to UTC, so two aware values written
      in different offsets still order correctly.

    This is a *comparison* helper only. It is never used to rewrite a stored
    value, so a store that holds naive timestamps keeps holding naive
    timestamps and round-trips byte for byte.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_timestamp(value: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime to an ISO-8601 string (``None`` passes through)."""
    if value is None:
        return None
    return value.isoformat()


def parse_timestamp(value: Optional[Any]) -> Optional[datetime]:
    """Parse an ISO-8601 string into a datetime (``None`` passes through)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


@dataclass
class MemoryRecord:
    """One believed fact in an agent memory store.

    The model is deliberately bitemporal-lite: ``written_at`` is when the agent
    learned the fact, while ``valid_from`` / ``valid_to`` describe the window in
    which the fact is believed to be true about the world. ``valid_to is None``
    means "still believed true", which is exactly the state that lets stale
    facts linger forever.

    Attributes:
        id: Stable unique identifier.
        entity: Subject of the fact, e.g. ``"user:1042"``.
        relation: Predicate, e.g. ``"preferred_language"``.
        value: Object of the fact, stored as free text.
        written_at: When the record was written to the store.
        valid_from: Start of the believed-true interval.
        valid_to: End of the believed-true interval, or ``None`` if open.
        fact_class: Coarse category driving TTL policy, e.g. ``"preference"``,
            ``"status"``, ``"identity"``, ``"ephemeral"``.
        last_retrieved_at: Last time this record was returned by a retrieval.
        retrieval_count: How many times it has been returned.
        source_session: Identifier of the session that wrote it.
    """

    id: str
    entity: str
    relation: str
    value: str
    written_at: datetime
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    fact_class: str = "status"
    last_retrieved_at: Optional[datetime] = None
    retrieval_count: int = 0
    source_session: Optional[str] = None
    merged_from: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.valid_from is None:
            self.valid_from = self.written_at

    @property
    def key(self) -> str:
        """The ``entity|relation`` grouping key."""
        return "%s|%s" % (self.entity, self.relation)

    def is_believed_true(self, now: datetime) -> bool:
        """Return True if the record is still an open/current belief at ``now``.

        Both sides go through :func:`_as_utc`, so a naive store compared
        against an aware clock (or the reverse) answers instead of raising.
        """
        moment = _as_utc(now)
        valid_from = _as_utc(self.valid_from)
        valid_to = _as_utc(self.valid_to)
        if valid_from is not None and valid_from > moment:
            return False
        return valid_to is None or valid_to > moment

    def age_days(self, now: datetime) -> float:
        """Age in days since ``written_at``."""
        return (_as_utc(now) - _as_utc(self.written_at)).total_seconds() / 86400.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        out: Dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, datetime):
                out[f.name] = format_timestamp(value)
            else:
                out[f.name] = value
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryRecord":
        """Rebuild a record from :meth:`to_dict` output."""
        return cls(
            id=str(data["id"]),
            entity=str(data["entity"]),
            relation=str(data["relation"]),
            value=str(data["value"]),
            written_at=parse_timestamp(data["written_at"]),
            valid_from=parse_timestamp(data.get("valid_from")),
            valid_to=parse_timestamp(data.get("valid_to")),
            fact_class=str(data.get("fact_class") or "status"),
            last_retrieved_at=parse_timestamp(data.get("last_retrieved_at")),
            retrieval_count=int(data.get("retrieval_count") or 0),
            source_session=data.get("source_session"),
            merged_from=list(data.get("merged_from") or []),
        )

    def copy(self) -> "MemoryRecord":
        """Return an independent copy (used by dry-run planning)."""
        return MemoryRecord.from_dict(self.to_dict())


class MemoryStore(abc.ABC):
    """Storage adapter interface.

    Implement these three methods against your own backend and the audit and
    maintenance passes will work unchanged.
    """

    @abc.abstractmethod
    def all_records(self) -> List[MemoryRecord]:
        """Return every record in the store."""

    @abc.abstractmethod
    def update(self, record: MemoryRecord) -> None:
        """Insert or replace ``record`` by its ``id``."""

    @abc.abstractmethod
    def delete(self, record_id: str) -> None:
        """Remove the record with ``record_id``. A missing id is not an error."""

    # -- convenience helpers built on the three primitives -------------------

    def add(self, record: MemoryRecord) -> None:
        """Alias for :meth:`update`, for readability at write sites."""
        self.update(record)

    def add_many(self, records: Iterable[MemoryRecord]) -> None:
        """Insert or replace several records."""
        for record in records:
            self.update(record)

    def get(self, record_id: str) -> Optional[MemoryRecord]:
        """Return one record by id, or ``None``."""
        for record in self.all_records():
            if record.id == record_id:
                return record
        return None

    def live_records(self, now: datetime) -> List[MemoryRecord]:
        """Return records still believed true at ``now``."""
        return [r for r in self.all_records() if r.is_believed_true(now)]

    def __len__(self) -> int:
        return len(self.all_records())

    def __iter__(self) -> Iterator[MemoryRecord]:
        return iter(self.all_records())


class JsonMemoryStore(MemoryStore):
    """Reference backend: all records in one JSON file.

    Args:
        path: File path. Created on first save if missing.
        write_through: When True (default) every mutation is persisted
            immediately. Set False for bulk loads and call :meth:`save` once.
    """

    def __init__(self, path: str, write_through: bool = True) -> None:
        self.path = path
        self.write_through = write_through
        self._records: Dict[str, MemoryRecord] = {}
        if os.path.exists(path):
            self.load()

    def load(self) -> None:
        """Re-read the backing file, discarding in-memory state."""
        with open(self.path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self._records = {
            str(item["id"]): MemoryRecord.from_dict(item)
            for item in payload.get("records", [])
        }

    def save(self) -> None:
        """Write the whole store back to disk."""
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        payload = {
            "version": 1,
            "records": [r.to_dict() for r in self.ordered_records()],
        }
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def ordered_records(self) -> List[MemoryRecord]:
        """Records sorted by id, for stable on-disk output."""
        return [self._records[k] for k in sorted(self._records)]

    def all_records(self) -> List[MemoryRecord]:
        return self.ordered_records()

    def update(self, record: MemoryRecord) -> None:
        self._records[record.id] = record
        if self.write_through:
            self.save()

    def delete(self, record_id: str) -> None:
        if record_id in self._records:
            del self._records[record_id]
            if self.write_through:
                self.save()


class SqliteMemoryStore(MemoryStore):
    """Reference backend: one SQLite table.

    Args:
        path: Database file path. Use ``":memory:"`` for an ephemeral store.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS memory_records (
        id               TEXT PRIMARY KEY,
        entity           TEXT NOT NULL,
        relation         TEXT NOT NULL,
        value            TEXT NOT NULL,
        written_at       TEXT NOT NULL,
        valid_from       TEXT,
        valid_to         TEXT,
        fact_class       TEXT NOT NULL,
        last_retrieved_at TEXT,
        retrieval_count  INTEGER NOT NULL DEFAULT 0,
        source_session   TEXT,
        merged_from      TEXT NOT NULL DEFAULT '[]'
    );
    """

    _COLUMNS = (
        "id",
        "entity",
        "relation",
        "value",
        "written_at",
        "valid_from",
        "valid_to",
        "fact_class",
        "last_retrieved_at",
        "retrieval_count",
        "source_session",
        "merged_from",
    )

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(self._SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        self.conn.close()

    def all_records(self) -> List[MemoryRecord]:
        cursor = self.conn.execute(
            "SELECT %s FROM memory_records ORDER BY id" % ", ".join(self._COLUMNS)
        )
        records = []
        for row in cursor.fetchall():
            data = dict(row)
            data["merged_from"] = json.loads(data.get("merged_from") or "[]")
            records.append(MemoryRecord.from_dict(data))
        return records

    def update(self, record: MemoryRecord) -> None:
        data = record.to_dict()
        data["merged_from"] = json.dumps(data.get("merged_from") or [])
        placeholders = ", ".join("?" for _ in self._COLUMNS)
        self.conn.execute(
            "INSERT OR REPLACE INTO memory_records (%s) VALUES (%s)"
            % (", ".join(self._COLUMNS), placeholders),
            tuple(data[column] for column in self._COLUMNS),
        )
        self.conn.commit()

    def delete(self, record_id: str) -> None:
        self.conn.execute("DELETE FROM memory_records WHERE id = ?", (record_id,))
        self.conn.commit()


def open_store(backend: str, path: str) -> MemoryStore:
    """Open one of the reference backends by name.

    Args:
        backend: ``"json"`` or ``"sqlite"``.
        path: Backing file path.

    Returns:
        A ready-to-use :class:`MemoryStore`.

    Raises:
        ValueError: If ``backend`` is not recognised.
    """
    if backend == "json":
        return JsonMemoryStore(path)
    if backend == "sqlite":
        return SqliteMemoryStore(path)
    raise ValueError("unknown backend: %r (expected 'json' or 'sqlite')" % backend)
