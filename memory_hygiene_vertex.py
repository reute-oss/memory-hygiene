# Disclaimer: This code is provided "as-is" as a demonstration only to illustrate a potential solution. The code does not constitute a Google product or service of any kind, and Google offers no support, warranties, or liability of any kind with its regard. Whoever chooses to use this code accepts all responsibility related to it, including for its implementation, use, and ongoing maintenance. For the avoidance of doubt, this code is not eligible for the Google Open Source Software Vulnerability Rewards Program.

"""Agent Platform Memory Bank backend for ``memory_hygiene``.

This module implements :class:`VertexMemoryBankStore`, a
:class:`memory_hygiene_store.MemoryStore` whose records live in an Agent
Platform Memory Bank (formerly Vertex AI Agent Engine Memory Bank, renamed
in April 2026) instead of a local JSON or SQLite file.

Verification status, read this before you deploy anything:
    Verified against a live Memory Bank on 7 October 2026, with
    google-cloud-aiplatform 2.4.0 in us-central1: construction (injected
    client and lazy default), ``update`` (create, and delete then create),
    ``all_records``, ``retrieve``, ``generate``, ``delete`` and an
    ``audit_store`` pass, with every structured field read back intact. The
    transcript is in ``RESULTS.md``. That is one run on one SDK version, not a
    support guarantee, and the Memory Bank surface is still moving.

    The first release of this file had never been run live and could not
    write at all, because its unit tests used a fake that accepted any
    argument. The fake in ``test_vertex_adapter.py`` is now strict, and checks
    its signatures and config fields against the installed SDK when the SDK
    is importable.

Design notes:
    The Google SDK is imported lazily, inside a helper, never at module import
    time. Importing this module on a machine with no ``google-cloud-aiplatform``
    installed is safe and does not raise. The error is raised only when the
    class is instantiated without an injected client or transport.

    Two fields the audit pass depends on do not exist in the Memory Bank data
    model and are deliberately reported as ``None`` rather than guessed. See
    the "Known gaps" section below.

Known gaps between the Memory Bank API and the ``MemoryRecord`` model:
    1. Retrieval telemetry. Memory Bank exposes no per record retrieval
       counter and no last accessed timestamp. ``retrieval_count`` and
       ``last_retrieved_at`` are therefore both set to ``None``, not to ``0``
       and not to a timestamp. ``memory_hygiene_audit.find_orphans`` looks for
       ``retrieval_count == 0 and last_retrieved_at is None``, so a ``None``
       counter makes orphan detection report nothing rather than report every
       record as an orphan. Orphan detection is unavailable on this backend
       until the caller supplies retrieval telemetry from its own logs.
    2. Point in time reads. Memory Bank has no point in time retrieval API:
       retrieve, list and get return current state, and no parameter asks
       for the state as of a past time. The service does keep immutable
       memory revisions, each with its ``fact`` and ``create_time``, so past
       state can be reconstructed client side, but this adapter does not do
       that. There is no validity interval on a memory, so ``valid_from`` and
       ``valid_to`` cannot be recovered from the service and
       ``MemoryStore.live_records(past_datetime)`` cannot be answered
       truthfully for the past. The adapter round trips both fields through
       custom metadata for records it wrote itself, and leaves them ``None``
       for memories the service generated.
    3. No update primitive. The documented surface is create, retrieve, list
       and delete. :meth:`VertexMemoryBankStore.update` is implemented as
       delete then create, which is not atomic and which mints a new server
       side resource id for the rewritten memory.
    4. Structure. A Memory Bank memory carries one free text ``fact`` string.
       ``entity``, ``relation``, ``value``, ``fact_class``, ``source_session``
       and ``merged_from`` have no native home, so the adapter stores them in
       custom metadata on write and reconstructs them on read. Memories
       produced by ``generate_memories`` carry no such metadata and fall back
       to a documented convention.
    5. Scope is not a filter on list. ``scope`` is an immutable map on each
       memory, limited to a small number of entries. ``list_memories`` is
       filtered with an EBNF filter string whose support for scope equality is
       not something this adapter relies on, so scope matching is applied
       client side after listing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

try:  # package layout: memory_hygiene/vertex.py
    from .store import MemoryRecord, MemoryStore
except ImportError:  # flat layout: memory_hygiene_vertex.py next to memory_hygiene_store.py
    from memory_hygiene_store import MemoryRecord, MemoryStore

__all__ = [
    "MemoryBankSDKNotAvailableError",
    "VertexMemoryBankStore",
    "DEFAULT_RELATION",
    "DEFAULT_FACT_CLASS",
    "METADATA_KEYS",
]

_LOGGER = logging.getLogger(__name__)

#: Relation used when a memory carries no ``mh_relation`` metadata, which is
#: the case for every memory produced by ``generate_memories``.
DEFAULT_RELATION = "fact"

#: Fact class used when a memory carries no ``mh_fact_class`` metadata. This
#: matches the ``MemoryRecord`` dataclass default. It is a convention of this
#: adapter, not a value returned by Memory Bank.
DEFAULT_FACT_CLASS = "status"

#: Custom metadata keys this adapter owns. Everything else in a memory's
#: metadata map is left untouched and is preserved on rewrite.
METADATA_KEYS: Tuple[str, ...] = (
    "mh_entity",
    "mh_relation",
    "mh_value",
    "mh_fact_class",
    "mh_written_at",
    "mh_valid_from",
    "mh_valid_to",
    "mh_source_session",
    "mh_merged_from",
)

_SDK_INSTALL_HINT = (
    "The Vertex AI SDK is not importable, so VertexMemoryBankStore cannot "
    "build a client. Either install the optional dependency from your clone of "
    'the repo, pip install -e ".[vertex]", or pip install google-cloud-aiplatform, '
    "or pass an already built client with client=... or an object exposing the "
    "Memory Bank methods with transport=... (that is how the unit tests run "
    "with no SDK and no network)."
)


class MemoryBankSDKNotAvailableError(RuntimeError):
    """Raised when the Vertex AI SDK is needed but cannot be imported."""


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping or from an object attribute.

    Memory Bank responses arrive either as plain dicts (REST, and the fake
    transport used in tests) or as protobuf backed objects with attributes.
    This helper hides that difference.

    Args:
        obj: A mapping or an arbitrary object.
        key: Field name to read.
        default: Value returned when the field is absent or empty.

    Returns:
        The field value, or ``default``.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    if value is None:
        return default
    return value


def _as_mapping(value: Any) -> Dict[str, Any]:
    """Coerce a protobuf map, a dict or ``None`` into a plain dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value)
    except (TypeError, ValueError):  # pragma: no cover, defensive only
        return {}


def _metadata_value(value: Any) -> Optional[str]:
    """Unwrap one custom metadata value into the plain string the adapter wrote.

    The API types every metadata value as a ``MemoryMetadataValue`` carrying
    exactly one of ``string_value``, ``bool_value``, ``double_value`` or
    ``timestamp_value``. Reading it with ``str()`` would yield the wrapper's
    repr, not the value. A bare string passes through unchanged.
    """
    if value is None or isinstance(value, str):
        return value
    for key in ("string_value", "timestamp_value", "double_value", "bool_value"):
        inner = _attr(value, key, None)
        if inner is not None:
            return inner.isoformat() if isinstance(inner, datetime) else str(inner)
    return None


def _parse_api_timestamp(value: Any, naive_utc: bool = True) -> Optional[datetime]:
    """Parse an RFC 3339 timestamp from the API into a ``datetime``.

    ``datetime.fromisoformat`` on Python 3.9 rejects a trailing ``Z`` and
    rejects more than six fractional digits, both of which the API can emit,
    so the string is normalised first.

    Args:
        value: A ``datetime``, an RFC 3339 string, or ``None``.
        naive_utc: When True, an aware datetime is converted to UTC and its
            tzinfo is dropped. The rest of the package compares timestamps
            against a plain ``datetime.utcnow()`` style value, and mixing
            aware and naive datetimes raises at comparison time.

    Returns:
        A ``datetime``, or ``None`` when the input was empty or unparsable.
    """
    if value is None or value == "":
        return None
    parsed: Optional[datetime] = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        if "." in text:
            head, _, tail = text.partition(".")
            digits = ""
            rest = ""
            for index, char in enumerate(tail):
                if char.isdigit():
                    digits += char
                else:
                    rest = tail[index:]
                    break
            text = head + "." + digits[:6] + rest
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            _LOGGER.warning("could not parse timestamp from API: %r", value)
            return None
    else:
        # Protobuf Timestamp objects expose ToDatetime().
        to_datetime = getattr(value, "ToDatetime", None)
        if callable(to_datetime):
            parsed = to_datetime()
        else:
            _LOGGER.warning("unsupported timestamp type from API: %r", type(value))
            return None
    if parsed is None:
        return None
    if naive_utc and parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _format_timestamp(value: Optional[datetime]) -> Optional[str]:
    """Serialise a datetime for custom metadata, ``None`` passes through."""
    if value is None:
        return None
    return value.isoformat()


class VertexMemoryBankStore(MemoryStore):
    """A ``MemoryStore`` backed by Agent Platform Memory Bank.

    The three abstract methods of :class:`memory_hygiene_store.MemoryStore`
    are implemented as follows:

    ===================  ====================================================
    ABC method           Memory Bank call
    ===================  ====================================================
    ``all_records()``    ``list_memories()``, paged, filtered by scope locally
    ``update(record)``   ``delete_memory()`` if present, then ``create_memory()``
    ``delete(id)``       ``delete_memory()``, a missing id is swallowed
    ===================  ====================================================

    Two Memory Bank calls have no ABC counterpart and are exposed as extra
    methods: :meth:`retrieve` for similarity search and :meth:`generate` for
    the automatic extraction pipeline.

    Attributes:
        project: Google Cloud project id.
        location: Agent Engine location, for example ``"us-central1"``.
        agent_engine_id: Agent Engine numeric id, or a full resource name.
        scope: Immutable scope map applied to every write and used to filter
            reads, for example ``{"user_id": "1042"}``.
    """

    def __init__(
        self,
        project: str,
        location: str,
        agent_engine_id: str,
        scope: Optional[Dict[str, str]] = None,
        user_id: str = "default",
        client: Any = None,
        transport: Any = None,
        naive_utc_timestamps: bool = True,
    ) -> None:
        """Build the store.

        Args:
            project: Google Cloud project id.
            location: Agent Engine location, for example ``"us-central1"``.
            agent_engine_id: Agent Engine numeric id, or an already formed
                ``projects/.../locations/.../reasoningEngines/...`` name.
            scope: Scope map for every read and write. Defaults to
                ``{"user_id": user_id}``. Memory Bank treats scope as an
                immutable key value map with a small maximum size.
            user_id: Used only to build the default scope.
            client: An already built SDK client, for example a
                ``vertexai.Client``. The adapter uses ``client.agent_engines``
                when that attribute exists, otherwise the object itself.
            transport: Any object exposing the Memory Bank methods directly.
                This is the injection point used by the unit tests, which run
                with no SDK and no network.
            naive_utc_timestamps: Convert aware API timestamps to naive UTC so
                they compare cleanly against the naive datetimes the audit
                pass uses.

        Raises:
            MemoryBankSDKNotAvailableError: If neither ``client`` nor
                ``transport`` was given and the Vertex AI SDK cannot be
                imported.
        """
        self.project = project
        self.location = location
        self.agent_engine_id = agent_engine_id
        self.scope: Dict[str, str] = dict(scope) if scope else {"user_id": user_id}
        self.naive_utc_timestamps = naive_utc_timestamps

        if transport is not None:
            self._api = transport
        elif client is not None:
            self._api = getattr(client, "agent_engines", client)
        else:
            self._api = self._build_default_api()

        # Maps a short record id back to the full Memory Bank resource name,
        # which delete_memory needs. Populated by every read.
        self._resource_names: Dict[str, str] = {}

    # -- construction -------------------------------------------------------

    def _build_default_api(self) -> Any:
        """Import the Vertex AI SDK lazily and return the Memory Bank surface.

        The import lives here, not at module top level, so that importing this
        module never fails on a machine without the SDK.

        Returns:
            The ``agent_engines`` surface of a freshly built client.

        Raises:
            MemoryBankSDKNotAvailableError: If the SDK cannot be imported or
                the client cannot be built.
        """
        try:
            import vertexai  # pylint: disable=import-outside-toplevel
        except ImportError as exc:
            raise MemoryBankSDKNotAvailableError(_SDK_INSTALL_HINT) from exc
        try:
            client = vertexai.Client(project=self.project, location=self.location)
        except Exception as exc:  # pragma: no cover, needs the real SDK
            raise MemoryBankSDKNotAvailableError(
                "The Vertex AI SDK is installed but building a client for "
                "project=%r location=%r failed: %s" % (self.project, self.location, exc)
            ) from exc
        return getattr(client, "agent_engines", client)

    @property
    def engine_name(self) -> str:
        """Full Agent Engine resource name used as the ``name`` argument."""
        if "/" in self.agent_engine_id:
            return self.agent_engine_id
        return "projects/%s/locations/%s/reasoningEngines/%s" % (
            self.project,
            self.location,
            self.agent_engine_id,
        )

    def _call(self, method_name: str, **kwargs: Any) -> Any:
        """Invoke one Memory Bank method on the injected or built surface.

        Args:
            method_name: One of ``generate_memories``, ``retrieve_memories``,
                ``create_memory``, ``list_memories``, ``delete_memory``.
            **kwargs: Passed straight through to the method.

        Returns:
            Whatever the method returned.

        Raises:
            AttributeError: If the transport does not expose the method, which
                is the signal that the SDK surface has moved and this adapter
                needs updating.
        """
        method = getattr(self._api, method_name, None)
        if method is None:
            raise AttributeError(
                "the Memory Bank transport %r does not expose %s(). The public "
                "surface may have changed, confirm it against the current SDK."
                % (type(self._api).__name__, method_name)
            )
        return method(**kwargs)

    # -- translation --------------------------------------------------------

    def _entity_from_scope(self, scope: Dict[str, Any]) -> str:
        """Derive an ``entity`` string from a Memory Bank scope map.

        ``{"user_id": "1042"}`` becomes ``"user:1042"``, which matches the
        convention used in the package docstrings. Any other scope shape is
        rendered as sorted ``key=value`` pairs so that it stays stable and
        groupable.
        """
        if not scope:
            return "unknown"
        if "user_id" in scope and len(scope) == 1:
            return "user:%s" % scope["user_id"]
        return ",".join("%s=%s" % (k, scope[k]) for k in sorted(scope))

    def memory_to_record(self, memory: Any) -> MemoryRecord:
        """Translate one Memory Bank memory into a :class:`MemoryRecord`.

        Fields the adapter wrote itself are recovered from custom metadata.
        Memories created by ``generate_memories`` carry no such metadata, so
        the whole ``fact`` string becomes ``value`` and the relation falls back
        to :data:`DEFAULT_RELATION`.

        Args:
            memory: A memory as returned by the API, dict or object.

        Returns:
            The translated record. ``retrieval_count`` and
            ``last_retrieved_at`` are always ``None`` on this backend.
        """
        # retrieve_memories returns each hit wrapped as {"memory": {...}} with
        # the similarity distance alongside it, list_memories returns the
        # memory directly. Unwrap so both paths share one translation.
        inner = _attr(memory, "memory", None)
        if inner is not None and _attr(memory, "fact", None) is None:
            memory = inner

        name = str(_attr(memory, "name", "") or "")
        record_id = name.rsplit("/", 1)[-1] if name else str(_attr(memory, "id", ""))
        if name:
            self._resource_names[record_id] = name

        fact = str(_attr(memory, "fact", "") or "")
        scope = _as_mapping(_attr(memory, "scope", {}))
        metadata = {
            key: _metadata_value(value)
            for key, value in _as_mapping(
                _attr(memory, "metadata", None) or _attr(memory, "custom_metadata", {})
            ).items()
        }

        written_at = _parse_api_timestamp(
            metadata.get("mh_written_at") or _attr(memory, "create_time", None),
            self.naive_utc_timestamps,
        )
        # Gap 2, part one: Memory Bank has no validity interval. valid_from and
        # valid_to are only recoverable for records this adapter wrote.
        valid_from = _parse_api_timestamp(
            metadata.get("mh_valid_from"), self.naive_utc_timestamps
        )
        valid_to = _parse_api_timestamp(
            metadata.get("mh_valid_to"), self.naive_utc_timestamps
        )

        merged_from_raw = metadata.get("mh_merged_from")
        merged_from: List[str] = []
        if merged_from_raw:
            try:
                decoded = json.loads(merged_from_raw)
                if isinstance(decoded, list):
                    merged_from = [str(item) for item in decoded]
            except (TypeError, ValueError):
                _LOGGER.warning("ignoring unparsable mh_merged_from on %s", record_id)

        record = MemoryRecord(
            id=record_id,
            entity=str(metadata.get("mh_entity") or self._entity_from_scope(scope)),
            relation=str(metadata.get("mh_relation") or DEFAULT_RELATION),
            value=str(metadata.get("mh_value") or fact),
            written_at=written_at,
            valid_from=valid_from,
            valid_to=valid_to,
            fact_class=str(metadata.get("mh_fact_class") or DEFAULT_FACT_CLASS),
            # Gap 1: Memory Bank exposes no last accessed timestamp. None, not
            # a guessed value, so orphan detection reports nothing instead of
            # reporting everything.
            last_retrieved_at=None,
            # Gap 1 again: no per record retrieval counter either. 0 would be a
            # claim that the memory was never read, which nobody measured.
            retrieval_count=None,
            # No originating session id is returned on a listed memory, so this
            # is only populated for records the adapter wrote.
            source_session=metadata.get("mh_source_session") or None,
            merged_from=merged_from,
        )
        return record

    def record_to_memory_kwargs(self, record: MemoryRecord) -> Dict[str, Any]:
        """Translate a :class:`MemoryRecord` into ``create_memory`` arguments.

        Args:
            record: The record to write.

        Returns:
            A kwargs dict with ``name``, ``fact``, ``scope`` and ``config``.
            Custom metadata is not a top level argument of ``create_memory``.
            It is a field of the config, and every value is typed, so each
            string is wrapped as ``{"string_value": ...}``.
        """
        metadata: Dict[str, str] = {
            "mh_entity": record.entity,
            "mh_relation": record.relation,
            "mh_value": record.value,
            "mh_fact_class": record.fact_class,
            "mh_merged_from": json.dumps(list(record.merged_from or [])),
        }
        for key, value in (
            ("mh_written_at", _format_timestamp(record.written_at)),
            ("mh_valid_from", _format_timestamp(record.valid_from)),
            ("mh_valid_to", _format_timestamp(record.valid_to)),
            ("mh_source_session", record.source_session),
        ):
            if value is not None:
                metadata[key] = str(value)
        return {
            "name": self.engine_name,
            "fact": self.format_fact(record),
            "scope": dict(self.scope),
            "config": {
                "metadata": {key: {"string_value": value} for key, value in metadata.items()}
            },
        }

    @staticmethod
    def format_fact(record: MemoryRecord) -> str:
        """Render a record as the single free text ``fact`` string.

        Memory Bank stores one string per memory and that string is what the
        model sees at recall time, so it is written in a readable form rather
        than as serialised JSON.
        """
        return "%s %s: %s" % (record.entity, record.relation, record.value)

    # -- MemoryStore abstract methods ---------------------------------------

    def all_records(self) -> List[MemoryRecord]:
        """Return every record in this store's scope.

        Pages through ``list_memories`` and filters by scope client side, see
        gap 5 in the module docstring.

        Returns:
            All translated records, in the order the API returned them.
        """
        records: List[MemoryRecord] = []
        for memory in self._iter_all_memories():
            scope = _as_mapping(_attr(memory, "scope", {}))
            if self.scope and not self._scope_matches(scope):
                continue
            records.append(self.memory_to_record(memory))
        return records

    def update(self, record: MemoryRecord) -> None:
        """Insert or replace ``record`` by its id.

        Memory Bank has no documented update primitive, so an existing memory
        is deleted and recreated. This is not atomic and the rewritten memory
        gets a new server side id, see gap 3 in the module docstring.

        Args:
            record: The record to write.
        """
        if record.id and self._lookup_resource_name(record.id):
            self.delete(record.id)
        created = self._call("create_memory", **self.record_to_memory_kwargs(record))
        # The SDK returns a long running operation. Its own ``name`` is the
        # operation's, the memory is under ``response``, so read that first.
        new_name = str(_attr(_attr(created, "response", None), "name", "") or "")
        if not new_name and "/operations/" not in str(_attr(created, "name", "") or ""):
            new_name = str(_attr(created, "name", "") or "")
        if new_name:
            self._resource_names[new_name.rsplit("/", 1)[-1]] = new_name

    def delete(self, record_id: str) -> None:
        """Remove the memory with ``record_id``. A missing id is not an error.

        Args:
            record_id: The short memory id, or a full resource name.
        """
        name = self._lookup_resource_name(record_id)
        if name is None:
            return
        self._call("delete_memory", name=name)
        self._resource_names.pop(record_id, None)
        self._resource_names.pop(name.rsplit("/", 1)[-1], None)

    # -- Memory Bank calls with no ABC counterpart --------------------------

    def retrieve(
        self,
        query: Optional[str] = None,
        top_k: int = 5,
        scope: Optional[Dict[str, str]] = None,
    ) -> List[MemoryRecord]:
        """Retrieve memories for the scope, optionally by similarity.

        Args:
            query: Natural language search query. When ``None`` the call is a
                plain scope fetch with no similarity search.
            top_k: Maximum number of similar memories to return.
            scope: Override for the store scope.

        Returns:
            Translated records. Note that retrieving through this method does
            not populate ``last_retrieved_at`` or ``retrieval_count`` on the
            service side, see gap 1.
        """
        kwargs: Dict[str, Any] = {
            "name": self.engine_name,
            "scope": dict(scope) if scope else dict(self.scope),
        }
        if query is not None:
            kwargs["similarity_search_params"] = {"search_query": query, "top_k": top_k}
        response = self._call("retrieve_memories", **kwargs)
        return [self.memory_to_record(memory) for memory in self._iter_memories(response)]

    def generate(
        self,
        direct_contents_source: Any = None,
        vertex_session_source: Any = None,
        scope: Optional[Dict[str, str]] = None,
        **extra: Any,
    ) -> Any:
        """Trigger Memory Bank extraction and consolidation.

        Args:
            direct_contents_source: Raw conversation turns to extract from.
            vertex_session_source: An Agent Engine session to extract from.
            scope: Override for the store scope.
            **extra: Forwarded unchanged, for options this adapter does not
                model, for example a config or wait flag.

        Returns:
            The raw API response. Generation is asynchronous on the service
            side, so nothing is translated here.
        """
        kwargs: Dict[str, Any] = {
            "name": self.engine_name,
            "scope": dict(scope) if scope else dict(self.scope),
        }
        if direct_contents_source is not None:
            kwargs["direct_contents_source"] = direct_contents_source
        if vertex_session_source is not None:
            kwargs["vertex_session_source"] = vertex_session_source
        kwargs.update(extra)
        return self._call("generate_memories", **kwargs)

    # -- internals ----------------------------------------------------------

    def _scope_matches(self, scope: Dict[str, Any]) -> bool:
        """Return True if ``scope`` contains every pair of the store scope."""
        return all(str(scope.get(key)) == str(value) for key, value in self.scope.items())

    def _lookup_resource_name(self, record_id: str) -> Optional[str]:
        """Resolve a short record id to a full resource name.

        Falls back to a listing pass when the id has not been seen yet, so a
        caller can delete by id without reading first.
        """
        if not record_id:
            return None
        if "/" in record_id:
            return record_id
        if record_id in self._resource_names:
            return self._resource_names[record_id]
        for memory in self._iter_all_memories():
            name = str(_attr(memory, "name", "") or "")
            if name:
                self._resource_names[name.rsplit("/", 1)[-1]] = name
        return self._resource_names.get(record_id)

    def _iter_all_memories(self) -> List[Any]:
        """Page through ``list_memories`` and return every memory.

        The SDK returns a pager that fetches every page itself and carries no
        ``next_page_token``, so with the SDK this loop makes exactly one call.
        The token path serves REST shaped transports, and the token goes inside
        ``config`` because ``list_memories`` has no top level ``page_token``.
        """
        memories: List[Any] = []
        page_token: Optional[str] = None
        seen_tokens = set()
        while True:
            kwargs: Dict[str, Any] = {"name": self.engine_name}
            if page_token:
                kwargs["config"] = {"page_token": page_token}
            response = self._call("list_memories", **kwargs)
            memories.extend(self._iter_memories(response))
            page_token = _attr(response, "next_page_token", None)
            if not page_token or page_token in seen_tokens:
                break
            seen_tokens.add(page_token)
        return memories

    @staticmethod
    def _iter_memories(response: Any) -> List[Any]:
        """Normalise the several shapes a memory listing can arrive in.

        Handles a bare list, a pager that is simply iterable, a mapping with a
        ``memories`` key, and an object with a ``memories`` attribute.
        """
        if response is None:
            return []
        if isinstance(response, dict):
            for key in ("memories", "retrieved_memories", "results"):
                if key in response:
                    return list(response[key] or [])
            return []
        for key in ("memories", "retrieved_memories", "results"):
            value = getattr(response, key, None)
            if value is not None:
                return list(value)
        if isinstance(response, (list, tuple)):
            return list(response)
        try:
            return list(response)
        except TypeError:  # pragma: no cover, defensive only
            return [response]

    def __repr__(self) -> str:
        return "VertexMemoryBankStore(engine=%r, scope=%r)" % (self.engine_name, self.scope)
