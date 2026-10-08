# Disclaimer: This code is provided "as-is" as a demonstration only to illustrate a potential solution. The code does not constitute a Google product or service of any kind, and Google offers no support, warranties, or liability of any kind with its regard. Whoever chooses to use this code accepts all responsibility related to it, including for its implementation, use, and ongoing maintenance. For the avoidance of doubt, this code is not eligible for the Google Open Source Software Vulnerability Rewards Program.

"""Unit tests for the Agent Platform Memory Bank adapter.

Everything here runs offline against :class:`FakeMemoryBankClient`, an in
process stand in for the Memory Bank surface. The fake is strict on purpose:

* it stands in for the nested ``memories`` surface the adapter calls, and
  every verb has explicit keyword only parameters copied from the real SDK
  signature, so an argument the real SDK would reject raises ``TypeError``;
* every ``config`` dict is checked against the real config type's field names,
  and every custom metadata value must be typed (``{"string_value": ...}``);
* responses have the real shapes: ``create`` returns an operation with the
  memory under ``response``, ``list`` returns an auto paging iterator with no
  page token, ``retrieve`` returns wrapped hits.

An earlier, permissive fake accepted any keyword argument, so the suite passed
while every live write failed. :class:`TestStrictFake` exists so that cannot
happen again. For each SDK that is importable (``agentplatform``, and the
older ``vertexai``) it also checks the fake's signatures and config fields
against the real ones.

Run it with:
    python3 test_memorybank_adapter.py
"""

from __future__ import annotations

import abc
import copy
import inspect
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_hygiene_audit import find_orphans  # noqa: E402
from memory_hygiene_store import MemoryRecord, MemoryStore  # noqa: E402
from memory_hygiene_memorybank import (  # noqa: E402
    DEFAULT_FACT_CLASS,
    DEFAULT_RELATION,
    MemoryBankSDKNotAvailableError,
    MemoryBankStore,
)

PROJECT = "demo-project"
LOCATION = "us-central1"
BANK_ID = "1234567890"
BANK_NAME = "projects/demo-project/locations/us-central1/reasoningEngines/1234567890"

#: Field names of each verb's config type. Identical in google-cloud-agentplatform
#: 2.3.0 (``MemoryConfig`` and friends) and google-cloud-aiplatform 2.4.0
#: (``AgentEngineMemoryConfig`` and friends). Checked against every installed
#: SDK by TestStrictFake.
CONFIG_FIELDS: Dict[str, frozenset] = {
    "create": frozenset({
        "description", "disable_memory_revisions", "display_name", "expire_time",
        "http_options", "memory_id", "metadata", "revision_expire_time",
        "revision_ttl", "topics", "ttl", "wait_for_completion",
    }),
    "list": frozenset({"filter", "http_options", "order_by", "page_size", "page_token"}),
    "retrieve": frozenset({"filter", "filter_groups", "http_options", "memory_types"}),
    "delete": frozenset({"http_options"}),
    "generate": frozenset({
        "allowed_topics", "disable_consolidation", "disable_memory_revisions",
        "http_options", "metadata", "metadata_merge_strategy", "revision_expire_time",
        "revision_labels", "revision_ttl", "wait_for_completion",
    }),
}
SIMILARITY_SEARCH_FIELDS = frozenset({"search_query", "top_k"})
SIMPLE_RETRIEVAL_FIELDS = frozenset({"page_size", "page_token"})
METADATA_VALUE_FIELDS = frozenset({"bool_value", "double_value", "string_value", "timestamp_value"})

#: The Memory Bank verbs the fake stands in for.
FAKED_METHODS = ("create", "list", "retrieve", "delete", "generate")


def _reject_unknown(where: str, value: Any, allowed: frozenset) -> None:
    """Mirror the SDK's pydantic models, which forbid extra fields."""
    if value is None:
        return
    if not isinstance(value, dict):
        raise TypeError("%s must be a dict, got %r" % (where, type(value).__name__))
    extra = sorted(set(value) - allowed)
    if extra:
        raise ValueError("%s: extra inputs are not permitted: %s" % (where, extra))


def _check_metadata(where: str, metadata: Any) -> None:
    """Every custom metadata value is a typed MemoryMetadataValue."""
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        raise TypeError("%s must be a dict" % where)
    for key, value in metadata.items():
        if not isinstance(value, dict) or len(value) != 1 or not set(value) <= METADATA_VALUE_FIELDS:
            raise ValueError(
                "%s[%r] must be a typed value with exactly one of %s, got %r"
                % (where, key, sorted(METADATA_VALUE_FIELDS), value)
            )


def _passed(**kwargs: Any) -> Dict[str, Any]:
    """The arguments a caller actually passed, defaults left out."""
    return {key: value for key, value in kwargs.items() if value is not None}


class FakePager:
    """Stands in for the SDK pager: iterates every item, exposes no page token."""

    def __init__(self, items: List[Dict[str, Any]]) -> None:
        self._items = items

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return iter(self._items)


class FakeMemoryBankClient:
    """Strict offline stand in for the nested Memory Bank ``memories`` surface.

    Records every call in :attr:`calls` as ``(method_name, kwargs)``, with only
    the arguments the caller actually passed. Keeps memories as dicts shaped
    like the API resource, with typed custom metadata.

    Args:
        memories: Initial memories.
        rest_paging: When True, ``list_memories`` returns REST shaped pages
            (``memories`` plus ``next_page_token``) of ``page_size`` items and
            reads the token from ``config``. When False (default) it returns an
            SDK shaped pager over everything, which is what the SDK does.
        page_size: Page size for ``rest_paging``.

    Note what this fake deliberately does NOT have, because the real service
    does not have it either: no retrieval counter and no last accessed
    timestamp.
    """

    def __init__(
        self,
        memories: Optional[List[Dict[str, Any]]] = None,
        rest_paging: bool = False,
        page_size: int = 2,
    ) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.memories: List[Dict[str, Any]] = copy.deepcopy(memories or [])
        self.rest_paging = rest_paging
        self.page_size = page_size
        self._counter = 500

    def _record_call(self, method: str, kwargs: Dict[str, Any]) -> None:
        self.calls.append((method, copy.deepcopy(kwargs)))

    def method_names(self) -> List[str]:
        """Names of the methods called so far, in order."""
        return [name for name, _ in self.calls]

    def kwargs_for(self, method: str) -> List[Dict[str, Any]]:
        """Every kwargs dict recorded for ``method``, in order."""
        return [kwargs for name, kwargs in self.calls if name == method]

    def create(
        self,
        *,
        name: str,
        fact: str,
        scope: Dict[str, str],
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        _reject_unknown("create config", config, CONFIG_FIELDS["create"])
        _check_metadata("create config.metadata", (config or {}).get("metadata"))
        self._record_call("create", _passed(name=name, fact=fact, scope=scope, config=config))
        self._counter += 1
        memory = {
            "name": "%s/memories/%d" % (name, self._counter),
            "fact": fact,
            "scope": dict(scope),
            "create_time": "2026-03-01T08:00:00.123456789Z",
            "update_time": "2026-03-01T08:00:00.123456789Z",
            "metadata": copy.deepcopy((config or {}).get("metadata") or {}),
        }
        self.memories.append(memory)
        # A long running operation: its own name, the memory under response.
        return {
            "name": "%s/operations/%d" % (memory["name"], 7000 + self._counter),
            "done": True,
            "response": copy.deepcopy(memory),
        }

    def list(self, *, name: str, config: Optional[Dict[str, Any]] = None) -> Any:
        _reject_unknown("list config", config, CONFIG_FIELDS["list"])
        self._record_call("list", _passed(name=name, config=config))
        if not self.rest_paging:
            return FakePager(copy.deepcopy(self.memories))
        token = (config or {}).get("page_token")
        start = int(token) if token else 0
        page = self.memories[start : start + self.page_size]
        response: Dict[str, Any] = {"memories": copy.deepcopy(page)}
        if start + self.page_size < len(self.memories):
            response["next_page_token"] = str(start + self.page_size)
        return response

    def retrieve(
        self,
        *,
        name: str,
        scope: Dict[str, str],
        similarity_search_params: Optional[Dict[str, Any]] = None,
        simple_retrieval_params: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        _reject_unknown("similarity_search_params", similarity_search_params, SIMILARITY_SEARCH_FIELDS)
        _reject_unknown("simple_retrieval_params", simple_retrieval_params, SIMPLE_RETRIEVAL_FIELDS)
        _reject_unknown("retrieve config", config, CONFIG_FIELDS["retrieve"])
        self._record_call(
            "retrieve",
            _passed(
                name=name,
                scope=scope,
                similarity_search_params=similarity_search_params,
                simple_retrieval_params=simple_retrieval_params,
                config=config,
            ),
        )
        hits = [m for m in self.memories if all(str(m["scope"].get(k)) == str(v) for k, v in scope.items())]
        if similarity_search_params:
            hits = hits[: int(similarity_search_params.get("top_k", 3))]
        # The retrieve surface wraps each hit and adds a distance.
        return [{"memory": copy.deepcopy(m), "distance": 0.21} for m in hits]

    def delete(self, *, name: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        _reject_unknown("delete config", config, CONFIG_FIELDS["delete"])
        self._record_call("delete", _passed(name=name, config=config))
        self.memories = [m for m in self.memories if m["name"] != name]
        return {"name": "%s/operations/1" % name, "done": True}

    def generate(
        self,
        *,
        name: str,
        vertex_session_source: Optional[Dict[str, Any]] = None,
        direct_contents_source: Optional[Dict[str, Any]] = None,
        direct_memories_source: Optional[Dict[str, Any]] = None,
        scope: Optional[Dict[str, str]] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        _reject_unknown("generate config", config, CONFIG_FIELDS["generate"])
        _check_metadata("generate config.metadata", (config or {}).get("metadata"))
        self._record_call(
            "generate",
            _passed(
                name=name,
                vertex_session_source=vertex_session_source,
                direct_contents_source=direct_contents_source,
                direct_memories_source=direct_memories_source,
                scope=scope,
                config=config,
            ),
        )
        return {"name": "%s/operations/9001" % name, "done": False}


class FakeAgentPlatformClient:
    """Shaped like ``agentplatform.Client``: verbs on ``memory_banks.memories``."""

    def __init__(self, memories: FakeMemoryBankClient, **_: Any) -> None:
        self.memory_banks = SimpleNamespace(memories=memories)


class FakeVertexClient:
    """Shaped like the older ``vertexai.Client``: verbs on ``agent_engines.memories``."""

    def __init__(self, memories: FakeMemoryBankClient, **_: Any) -> None:
        self.agent_engines = SimpleNamespace(memories=memories)


def fake_sdk_module(name: str, client_cls: type, memories: FakeMemoryBankClient) -> Any:
    """A stand in SDK module whose ``Client(project=..., location=...)`` works offline."""
    module = type(sys)(name)
    module.Client = lambda **kwargs: client_cls(memories, **kwargs)  # type: ignore[attr-defined]
    return module


class _BlockImports:
    """Context manager that makes chosen top level packages unimportable."""

    def __init__(self, *roots: str) -> None:
        self.roots = set(roots)
        self._saved: Dict[str, Any] = {}

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if fullname.split(".")[0] in self.roots:
            raise ImportError("blocked by test: %s" % fullname)
        return None

    def __enter__(self) -> "_BlockImports":
        for name in list(sys.modules):
            if name.split(".")[0] in self.roots:
                self._saved[name] = sys.modules.pop(name)
        sys.meta_path.insert(0, self)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        sys.meta_path.remove(self)
        sys.modules.update(self._saved)


def typed(**values: str) -> Dict[str, Dict[str, str]]:
    """Custom metadata as the API returns it: every value typed."""
    return {key: {"string_value": value} for key, value in values.items()}


def seeded_memories() -> List[Dict[str, Any]]:
    """Two memories written by the adapter plus one generated by the service."""
    return [
        {
            "name": BANK_NAME + "/memories/m1",
            "fact": "user:1042 preferred_language: Hebrew",
            "scope": {"user_id": "1042"},
            "create_time": "2026-01-05T09:30:00Z",
            "update_time": "2026-01-05T09:30:00Z",
            "metadata": typed(
                mh_entity="user:1042",
                mh_relation="preferred_language",
                mh_value="Hebrew",
                mh_fact_class="preference",
                mh_written_at="2026-01-05T09:30:00",
                mh_source_session="sess-7",
                mh_merged_from='["old-1", "old-2"]',
            ),
        },
        {
            "name": BANK_NAME + "/memories/m2",
            "fact": "user:1042 employer: Acme",
            "scope": {"user_id": "1042"},
            "create_time": "2026-02-10T12:00:00Z",
            "update_time": "2026-02-10T12:00:00Z",
            "metadata": typed(
                mh_entity="user:1042",
                mh_relation="employer",
                mh_value="Acme",
                mh_fact_class="status",
                mh_written_at="2026-02-10T12:00:00",
                mh_valid_to="2026-06-01T00:00:00",
            ),
        },
        {
            # Produced by generate_memories, so no adapter metadata at all.
            "name": BANK_NAME + "/memories/m3",
            "fact": "The user is allergic to peanuts.",
            "scope": {"user_id": "1042"},
            "create_time": "2026-02-20T18:45:00Z",
            "update_time": "2026-02-20T18:45:00Z",
        },
        {
            # Different scope, must not leak into this store's reads.
            "name": BANK_NAME + "/memories/m4",
            "fact": "user:9999 employer: Globex",
            "scope": {"user_id": "9999"},
            "create_time": "2026-02-21T18:45:00Z",
            "update_time": "2026-02-21T18:45:00Z",
        },
    ]


def make_store(**kwargs: Any) -> Tuple[MemoryBankStore, FakeMemoryBankClient]:
    """Build a store wired to a fake transport seeded with memories."""
    fake = FakeMemoryBankClient(
        kwargs.pop("memories", seeded_memories()), rest_paging=kwargs.pop("rest_paging", False)
    )
    if "scope" not in kwargs:
        kwargs.setdefault("user_id", "1042")
    store = MemoryBankStore(
        project=PROJECT,
        location=LOCATION,
        memory_bank=BANK_ID,
        transport=fake,
        **kwargs
    )
    return store, fake


def a_record(**overrides: Any) -> MemoryRecord:
    fields = dict(id="", entity="user:1042", relation="tier", value="gold", written_at=datetime(2026, 3, 1, 8, 0, 0))
    fields.update(overrides)
    return MemoryRecord(**fields)


#: Run in a subprocess so importing an SDK never leaks into this process,
#: where TestConstruction asserts no SDK has been imported. One script per SDK:
#: the nested ``memories`` class and its config types differ only in names.
_SDK_INTROSPECTION = r"""
import inspect, json, sys, warnings
warnings.simplefilter("ignore")
sdk = sys.argv[1]
if sdk == "agentplatform":
    from agentplatform._genai.memories import Memories
    from agentplatform._genai.types import common as t
    configs = {"create": t.MemoryConfig, "list": t.ListMemoriesConfig,
               "retrieve": t.RetrieveMemoriesConfig, "delete": t.DeleteMemoryConfig,
               "generate": t.GenerateMemoriesConfig}
else:
    from vertexai._genai.memories import Memories
    from vertexai._genai.types import common as t
    configs = {"create": t.AgentEngineMemoryConfig, "list": t.ListAgentEngineMemoryConfig,
               "retrieve": t.RetrieveAgentEngineMemoriesConfig,
               "delete": t.DeleteAgentEngineMemoryConfig,
               "generate": t.GenerateAgentEngineMemoriesConfig}
methods = {}
for name in %(methods)r:
    sig = inspect.signature(getattr(Memories, name))
    methods[name] = [
        [p.name, p.kind.name, p.default is inspect.Parameter.empty]
        for p in sig.parameters.values() if p.name != "self"
    ]
print(json.dumps({
    "methods": methods,
    "configs": {k: sorted(v.model_fields) for k, v in configs.items()},
    "similarity": sorted(t.RetrieveMemoriesRequestSimilaritySearchParams.model_fields),
    "simple": sorted(t.RetrieveMemoriesRequestSimpleRetrievalParams.model_fields),
    "metadata_value": sorted(t.MemoryMetadataValue.model_fields),
}))
""" % {"methods": FAKED_METHODS}

_sdk_cache: Dict[str, Any] = {}


def real_sdk_surface(sdk: str) -> Optional[Dict[str, Any]]:
    """Signatures and config fields of one installed SDK, or None if absent."""
    if sdk not in _sdk_cache:
        proc = subprocess.run(
            [sys.executable, "-c", _SDK_INTROSPECTION, sdk], capture_output=True, text=True
        )
        _sdk_cache[sdk] = json.loads(proc.stdout) if proc.returncode == 0 else None
    return _sdk_cache[sdk]


class TestConstruction(unittest.TestCase):
    """Import safety, lazy SDK loading and constructor wiring."""

    def test_module_import_does_not_pull_in_the_sdk(self) -> None:
        self.assertNotIn("agentplatform", sys.modules)
        self.assertNotIn("vertexai", sys.modules)
        self.assertNotIn("google.cloud.aiplatform", sys.modules)

    def test_construction_without_sdk_fails_cleanly(self) -> None:
        with _BlockImports("agentplatform", "vertexai", "google"):
            with self.assertRaises(MemoryBankSDKNotAvailableError) as caught:
                MemoryBankStore(PROJECT, LOCATION, BANK_ID)
        message = str(caught.exception)
        self.assertIn("google-cloud-agentplatform", message)
        self.assertIn("transport=", message)
        self.assertIn("Python 3.10", message)

    def test_construction_with_injected_transport(self) -> None:
        store, _ = make_store()
        self.assertIsInstance(store, MemoryStore)
        self.assertEqual(store.memory_bank_name, BANK_NAME)
        self.assertEqual(store.surface, "injected transport")

    def test_injected_agentplatform_client_uses_memory_banks_memories(self) -> None:
        fake = FakeMemoryBankClient(seeded_memories())
        store = MemoryBankStore(
            PROJECT, LOCATION, BANK_ID, client=FakeAgentPlatformClient(fake), user_id="1042"
        )
        self.assertEqual(store.surface, "agentplatform memory_banks.memories")
        self.assertEqual(len(store.all_records()), 3)

    def test_injected_vertexai_client_uses_agent_engines_memories(self) -> None:
        fake = FakeMemoryBankClient(seeded_memories())
        store = MemoryBankStore(
            PROJECT, LOCATION, BANK_ID, client=FakeVertexClient(fake), user_id="1042"
        )
        self.assertTrue(store.surface.startswith("vertexai agent_engines.memories"))
        self.assertEqual(len(store.all_records()), 3)

    def test_default_construction_prefers_agentplatform(self) -> None:
        fake = FakeMemoryBankClient(seeded_memories())
        modules = {
            "agentplatform": fake_sdk_module("agentplatform", FakeAgentPlatformClient, fake),
            "vertexai": fake_sdk_module("vertexai", FakeVertexClient, FakeMemoryBankClient()),
        }
        with _BlockImports("agentplatform", "vertexai"):
            sys.modules.update(modules)
            try:
                with self.assertLogs("memory_hygiene_memorybank", level="INFO") as logs:
                    store = MemoryBankStore(PROJECT, LOCATION, BANK_ID, user_id="1042")
            finally:
                for name in modules:
                    sys.modules.pop(name, None)
        self.assertEqual(store.surface, "agentplatform memory_banks.memories")
        self.assertEqual(len(store.all_records()), 3)
        self.assertTrue(any("agentplatform memory_banks.memories" in line for line in logs.output))

    def test_default_construction_falls_back_to_vertexai_and_says_so(self) -> None:
        fake = FakeMemoryBankClient(seeded_memories())
        with _BlockImports("agentplatform", "vertexai"):
            sys.modules["vertexai"] = fake_sdk_module("vertexai", FakeVertexClient, fake)
            try:
                with self.assertLogs("memory_hygiene_memorybank", level="WARNING") as logs:
                    store = MemoryBankStore(PROJECT, LOCATION, BANK_ID, user_id="1042")
            finally:
                sys.modules.pop("vertexai", None)
        self.assertTrue(store.surface.startswith("vertexai agent_engines.memories"))
        self.assertEqual(len(store.all_records()), 3)
        self.assertTrue(any("falling back to the deprecated vertexai SDK" in line for line in logs.output))

    def test_full_resource_name_is_accepted_as_engine_id(self) -> None:
        store = MemoryBankStore(
            PROJECT, LOCATION, BANK_NAME, transport=FakeMemoryBankClient()
        )
        self.assertEqual(store.memory_bank_name, BANK_NAME)

    def test_no_abstract_methods_left_unimplemented(self) -> None:
        self.assertEqual(getattr(MemoryBankStore, "__abstractmethods__", frozenset()), frozenset())
        for name in ("all_records", "update", "delete"):
            self.assertTrue(callable(getattr(MemoryBankStore, name)))

    def test_abc_signatures_match_the_base_class(self) -> None:
        for name in ("all_records", "update", "delete"):
            base = inspect.signature(getattr(MemoryStore, name))
            impl = inspect.signature(getattr(MemoryBankStore, name))
            self.assertEqual(str(base), str(impl), name)


class TestScope(unittest.TestCase):
    """Scope defaulting, pass through and client side filtering."""

    def test_default_scope_is_user_id(self) -> None:
        store, _ = make_store(user_id="1042")
        self.assertEqual(store.scope, {"user_id": "1042"})
        bare = MemoryBankStore(
            PROJECT, LOCATION, BANK_ID, transport=FakeMemoryBankClient()
        )
        self.assertEqual(bare.scope, {"user_id": "default"})

    def test_explicit_scope_is_used_verbatim(self) -> None:
        store, _ = make_store(scope={"user_id": "1042", "app": "support"})
        self.assertEqual(store.scope, {"user_id": "1042", "app": "support"})

    def test_scope_is_passed_through_on_write(self) -> None:
        store, fake = make_store(scope={"user_id": "1042", "app": "support"})
        store.update(a_record())
        created = fake.kwargs_for("create")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["scope"], {"user_id": "1042", "app": "support"})
        self.assertEqual(created[0]["name"], BANK_NAME)

    def test_scope_is_passed_through_on_retrieve(self) -> None:
        store, fake = make_store(user_id="1042")
        store.retrieve(query="what language", top_k=2)
        kwargs = fake.kwargs_for("retrieve")[0]
        self.assertEqual(kwargs["scope"], {"user_id": "1042"})
        self.assertEqual(
            kwargs["similarity_search_params"], {"search_query": "what language", "top_k": 2}
        )

    def test_other_scopes_are_filtered_out_of_all_records(self) -> None:
        store, _ = make_store(user_id="1042")
        ids = sorted(r.id for r in store.all_records())
        self.assertEqual(ids, ["m1", "m2", "m3"])  # m4 belongs to user 9999


class TestTranslation(unittest.TestCase):
    """Memory Bank memory to MemoryRecord translation."""

    def test_adapter_written_memory_round_trips(self) -> None:
        store, _ = make_store()
        record = {r.id: r for r in store.all_records()}["m1"]
        self.assertEqual(record.entity, "user:1042")
        self.assertEqual(record.relation, "preferred_language")
        self.assertEqual(record.value, "Hebrew")
        self.assertEqual(record.fact_class, "preference")
        self.assertEqual(record.written_at, datetime(2026, 1, 5, 9, 30, 0))
        self.assertEqual(record.source_session, "sess-7")
        self.assertEqual(record.merged_from, ["old-1", "old-2"])
        self.assertIsNone(record.written_at.tzinfo)

    def test_typed_metadata_objects_are_unwrapped(self) -> None:
        # The SDK hands back MemoryMetadataValue objects, not dicts.
        memory = seeded_memories()[0]
        memory["metadata"] = {
            key: SimpleNamespace(string_value=value["string_value"], bool_value=None)
            for key, value in memory["metadata"].items()
        }
        store, _ = make_store(memories=[memory])
        record = store.all_records()[0]
        self.assertEqual((record.entity, record.relation, record.value), ("user:1042", "preferred_language", "Hebrew"))
        self.assertEqual(record.written_at, datetime(2026, 1, 5, 9, 30, 0))

    def test_generated_memory_falls_back_to_the_documented_convention(self) -> None:
        store, _ = make_store()
        record = {r.id: r for r in store.all_records()}["m3"]
        self.assertEqual(record.entity, "user:1042")  # derived from scope
        self.assertEqual(record.relation, DEFAULT_RELATION)
        self.assertEqual(record.value, "The user is allergic to peanuts.")
        self.assertEqual(record.fact_class, DEFAULT_FACT_CLASS)
        self.assertEqual(record.written_at, datetime(2026, 2, 20, 18, 45, 0))
        self.assertIsNone(record.source_session)
        self.assertEqual(record.merged_from, [])

    def test_valid_to_round_trips_through_metadata(self) -> None:
        store, _ = make_store()
        record = {r.id: r for r in store.all_records()}["m2"]
        self.assertEqual(record.valid_to, datetime(2026, 6, 1, 0, 0, 0))

    def test_rfc3339_nanoseconds_and_z_suffix_parse(self) -> None:
        store, fake = make_store(memories=[])
        store.update(a_record())
        fake.memories[0].pop("metadata")  # force the create_time path
        record = store.all_records()[0]
        self.assertEqual(record.written_at, datetime(2026, 3, 1, 8, 0, 0, 123456))

    def test_fact_string_is_human_readable(self) -> None:
        store, fake = make_store(memories=[])
        store.update(a_record())
        self.assertEqual(fake.kwargs_for("create")[0]["fact"], "user:1042 tier: gold")

    def test_record_to_memory_kwargs_carries_every_structured_field(self) -> None:
        store, _ = make_store(memories=[])
        record = a_record(
            id="x",
            valid_to=datetime(2026, 9, 1, 0, 0, 0),
            fact_class="preference",
            source_session="sess-12",
            merged_from=["a", "b"],
        )
        kwargs = store.record_to_memory_kwargs(record)
        self.assertNotIn("metadata", kwargs)  # not a create_memory argument
        metadata = kwargs["config"]["metadata"]
        self.assertEqual(metadata["mh_entity"], {"string_value": "user:1042"})
        self.assertEqual(metadata["mh_relation"], {"string_value": "tier"})
        self.assertEqual(metadata["mh_value"], {"string_value": "gold"})
        self.assertEqual(metadata["mh_fact_class"], {"string_value": "preference"})
        self.assertEqual(metadata["mh_valid_to"], {"string_value": "2026-09-01T00:00:00"})
        self.assertEqual(metadata["mh_source_session"], {"string_value": "sess-12"})
        self.assertEqual(metadata["mh_merged_from"], {"string_value": '["a", "b"]'})

    def test_write_then_read_round_trips_every_field(self) -> None:
        store, _ = make_store(memories=[])
        written = a_record(
            valid_to=datetime(2026, 9, 1, 0, 0, 0),
            fact_class="preference",
            source_session="sess-12",
            merged_from=["a", "b"],
        )
        store.update(written)
        read = store.all_records()[0]
        for field in ("entity", "relation", "value", "fact_class", "written_at", "valid_to", "source_session", "merged_from"):
            self.assertEqual(getattr(read, field), getattr(written, field), field)


class TestKnownGaps(unittest.TestCase):
    """The two gaps must surface as None, never as invented values."""

    def test_retrieval_telemetry_is_none_not_zero(self) -> None:
        store, _ = make_store()
        for record in store.all_records():
            self.assertIsNone(record.retrieval_count, record.id)
            self.assertIsNone(record.last_retrieved_at, record.id)

    def test_retrieving_does_not_invent_a_last_accessed_timestamp(self) -> None:
        store, _ = make_store()
        for record in store.retrieve(query="language", top_k=3):
            self.assertIsNone(record.retrieval_count, record.id)
            self.assertIsNone(record.last_retrieved_at, record.id)

    def test_orphan_detection_reports_nothing_rather_than_everything(self) -> None:
        store, _ = make_store()
        now = datetime(2026, 9, 1, 0, 0, 0)
        live = store.live_records(now)
        self.assertTrue(live)
        self.assertEqual(find_orphans(live, now, grace_days=14.0), [])

    def test_no_point_in_time_read_is_offered(self) -> None:
        store, _ = make_store()
        params = inspect.signature(MemoryBankStore.all_records).parameters
        # The service keeps memory revisions, but has no as of retrieval
        # parameter, and this adapter does not reconstruct past state.
        self.assertEqual(list(params), ["self"])
        generated = {r.id: r for r in store.all_records()}["m3"]
        self.assertIsNone(generated.valid_to)

    def test_generated_memory_has_no_validity_interval_from_the_service(self) -> None:
        store, _ = make_store()
        generated = {r.id: r for r in store.all_records()}["m3"]
        # valid_from is filled in by MemoryRecord.__post_init__ from written_at,
        # it is a package convention, not a value Memory Bank returned.
        self.assertEqual(generated.valid_from, generated.written_at)


class TestAbcMethods(unittest.TestCase):
    """Round trips through all_records, update and delete."""

    def test_all_records_makes_one_call_against_an_sdk_pager(self) -> None:
        store, fake = make_store()
        self.assertEqual(len(store.all_records()), 3)
        self.assertEqual(fake.kwargs_for("list"), [{"name": BANK_NAME}])

    def test_rest_paging_passes_the_token_inside_config(self) -> None:
        store, fake = make_store(rest_paging=True)
        self.assertEqual(len(store.all_records()), 3)
        self.assertEqual(
            fake.kwargs_for("list"),
            [{"name": BANK_NAME}, {"name": BANK_NAME, "config": {"page_token": "2"}}],
        )

    def test_update_inserts_a_new_memory(self) -> None:
        store, fake = make_store(memories=[])
        store.update(a_record())
        self.assertEqual(len(fake.memories), 1)
        self.assertEqual(fake.kwargs_for("delete"), [])
        read_back = store.all_records()
        self.assertEqual(len(read_back), 1)
        self.assertEqual(read_back[0].value, "gold")

    def test_update_records_the_memory_name_not_the_operation_name(self) -> None:
        store, fake = make_store(memories=[])
        store.update(a_record())
        memory_name = fake.memories[0]["name"]
        self.assertEqual(store._resource_names, {memory_name.rsplit("/", 1)[-1]: memory_name})

    def test_update_of_an_existing_memory_deletes_then_creates(self) -> None:
        store, fake = make_store()
        existing = {r.id: r for r in store.all_records()}["m2"]
        existing.value = "Initech"
        store.update(existing)
        self.assertEqual(fake.kwargs_for("delete")[0]["name"], BANK_NAME + "/memories/m2")
        self.assertEqual(fake.method_names()[-2:], ["delete", "create"])
        values = sorted(r.value for r in store.all_records())
        self.assertEqual(values, ["Hebrew", "Initech", "The user is allergic to peanuts."])
        # The rewritten memory got a new server side id, see gap 3.
        self.assertNotIn("m2", [r.id for r in store.all_records()])

    def test_delete_uses_the_full_resource_name(self) -> None:
        store, fake = make_store()
        store.all_records()
        store.delete("m1")
        self.assertEqual(fake.kwargs_for("delete"), [{"name": BANK_NAME + "/memories/m1"}])
        self.assertEqual(sorted(r.id for r in store.all_records()), ["m2", "m3"])

    def test_delete_resolves_an_id_it_has_not_read_yet(self) -> None:
        store, fake = make_store()
        store.delete("m3")
        self.assertEqual(fake.kwargs_for("delete"), [{"name": BANK_NAME + "/memories/m3"}])

    def test_delete_of_a_missing_id_is_not_an_error(self) -> None:
        store, fake = make_store()
        store.delete("does-not-exist")
        self.assertEqual(fake.kwargs_for("delete"), [])

    def test_inherited_helpers_work_on_top_of_the_three_methods(self) -> None:
        store, _ = make_store()
        self.assertEqual(len(store), 3)
        self.assertEqual(store.get("m1").relation, "preferred_language")
        self.assertIsNone(store.get("nope"))
        self.assertEqual(len(list(iter(store))), 3)

    def test_add_many_writes_every_record(self) -> None:
        store, fake = make_store(memories=[])
        store.add_many(
            [
                MemoryRecord("", "user:1042", "tier", "gold", datetime(2026, 3, 1, 8, 0, 0)),
                MemoryRecord("", "user:1042", "city", "Tel Aviv", datetime(2026, 3, 2, 8, 0, 0)),
            ]
        )
        self.assertEqual(len(fake.kwargs_for("create")), 2)
        self.assertEqual(sorted(r.value for r in store.all_records()), ["Tel Aviv", "gold"])


class TestExtraCalls(unittest.TestCase):
    """The two Memory Bank calls with no ABC counterpart."""

    def test_retrieve_without_a_query_is_a_plain_scope_fetch(self) -> None:
        store, fake = make_store()
        records = store.retrieve()
        self.assertNotIn("similarity_search_params", fake.kwargs_for("retrieve")[0])
        self.assertEqual(sorted(r.id for r in records), ["m1", "m2", "m3"])

    def test_retrieve_unwraps_the_memory_and_distance_envelope(self) -> None:
        store, _ = make_store()
        record = {r.id: r for r in store.retrieve(query="language", top_k=3)}["m1"]
        self.assertEqual(record.value, "Hebrew")

    def test_generate_forwards_sources_scope_and_config(self) -> None:
        store, fake = make_store(user_id="1042")
        store.generate(direct_contents_source={"events": []}, config={"wait_for_completion": True})
        kwargs = fake.kwargs_for("generate")[0]
        self.assertEqual(kwargs["name"], BANK_NAME)
        self.assertEqual(kwargs["scope"], {"user_id": "1042"})
        self.assertEqual(kwargs["direct_contents_source"], {"events": []})
        self.assertEqual(kwargs["config"], {"wait_for_completion": True})

    def test_generate_rejects_an_option_the_sdk_does_not_have(self) -> None:
        store, _ = make_store(user_id="1042")
        with self.assertRaises(TypeError):
            store.generate(direct_contents_source={"events": []}, extra_option=True)

    def test_missing_transport_method_fails_loudly(self) -> None:
        class Bare:
            pass

        store = MemoryBankStore(PROJECT, LOCATION, BANK_ID, transport=Bare())
        with self.assertRaises(AttributeError) as caught:
            store.all_records()
        self.assertIn("list()", str(caught.exception))


class TestStrictFake(unittest.TestCase):
    """The fake must reject what the real SDK rejects, or the suite is circular."""

    def test_metadata_as_a_top_level_argument_is_rejected(self) -> None:
        # The exact call shape the first release made. It failed live with
        # "create_memory() got an unexpected keyword argument 'metadata'".
        fake = FakeMemoryBankClient()
        with self.assertRaises(TypeError):
            fake.create(name=BANK_NAME, fact="f", scope={"user_id": "1"}, metadata={"k": "v"})

    def test_an_adapter_sending_the_old_write_shape_fails(self) -> None:
        store, _ = make_store(memories=[])
        correct = store.record_to_memory_kwargs

        def old_shape(record: MemoryRecord) -> Dict[str, Any]:
            kwargs = correct(record)
            config = kwargs.pop("config")
            kwargs["metadata"] = {k: v["string_value"] for k, v in config["metadata"].items()}
            return kwargs

        store.record_to_memory_kwargs = old_shape  # type: ignore[assignment]
        with self.assertRaises(TypeError):
            store.update(a_record())

    def test_untyped_metadata_values_are_rejected(self) -> None:
        fake = FakeMemoryBankClient()
        with self.assertRaises(ValueError):
            fake.create(
                name=BANK_NAME, fact="f", scope={"user_id": "1"}, config={"metadata": {"k": "v"}}
            )

    def test_unknown_config_fields_are_rejected(self) -> None:
        fake = FakeMemoryBankClient()
        with self.assertRaises(ValueError):
            fake.delete(name=BANK_NAME + "/memories/m1", config={"wait_for_completion": True})
        with self.assertRaises(ValueError):
            fake.list(name=BANK_NAME, config={"scope": {"user_id": "1"}})

    def test_page_token_as_a_top_level_argument_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            FakeMemoryBankClient().list(name=BANK_NAME, page_token="2")

    def test_positional_arguments_are_rejected(self) -> None:
        # The real methods are keyword only.
        with self.assertRaises(TypeError):
            FakeMemoryBankClient().delete(BANK_NAME + "/memories/m1")

    def _check_signatures(self, sdk: str) -> None:
        surface = real_sdk_surface(sdk)
        if surface is None:
            self.skipTest("%s not importable, signature check skipped" % sdk)
        for name in FAKED_METHODS:
            fake_params = [
                [p.name, p.kind.name, p.default is inspect.Parameter.empty]
                for p in inspect.signature(getattr(FakeMemoryBankClient, name)).parameters.values()
                if p.name != "self"
            ]
            self.assertEqual(fake_params, surface["methods"][name], "%s %s" % (sdk, name))

    def _check_config_fields(self, sdk: str) -> None:
        surface = real_sdk_surface(sdk)
        if surface is None:
            self.skipTest("%s not importable, config field check skipped" % sdk)
        for name in FAKED_METHODS:
            self.assertEqual(sorted(CONFIG_FIELDS[name]), surface["configs"][name], "%s %s" % (sdk, name))
        self.assertEqual(sorted(SIMILARITY_SEARCH_FIELDS), surface["similarity"], sdk)
        self.assertEqual(sorted(SIMPLE_RETRIEVAL_FIELDS), surface["simple"], sdk)
        self.assertEqual(sorted(METADATA_VALUE_FIELDS), surface["metadata_value"], sdk)

    def test_fake_signatures_match_agentplatform(self) -> None:
        self._check_signatures("agentplatform")

    def test_fake_config_fields_match_agentplatform(self) -> None:
        self._check_config_fields("agentplatform")

    def test_fake_signatures_match_vertexai(self) -> None:
        self._check_signatures("vertexai")

    def test_fake_config_fields_match_vertexai(self) -> None:
        self._check_config_fields("vertexai")


if __name__ == "__main__":
    unittest.main(verbosity=2)
