# Memory Bank adapter: where the API did not line up

Artifacts in this folder:
- `memory_hygiene_memorybank.py` : the `MemoryBankStore(MemoryStore)` adapter. Named
  `memory_hygiene_vertex.py` and `VertexMemoryBankStore` until 7 October 2026.
- `test_memorybank_adapter.py`   : 53 unittest cases against a STRICT fake transport that
  mirrors the real SDK signatures and rejects unknown arguments. Without an SDK, 49
  pass and 4 skip. With google-cloud-agentplatform 2.3.0 alone, 51 pass and 2 skip.
  With google-cloud-aiplatform 2.4.0, which ships both SDK modules, all 53 pass.
- `examples/adk_agent.py`        : ADK wiring example (not executed anywhere)

Python: the core package is standard library only and runs on Python 3.9 and up.
The Memory Bank extra (`pip install -e ".[memorybank]"`) needs Python 3.10 or newer,
because both Google SDKs do. On 3.9, pip skips the SDK without an error, and the
adapter reports the missing SDK when it is constructed.

Status: verified against a live Memory Bank on 7 October 2026, one run each:
- google-cloud-agentplatform 2.3.0, on a bank made by `memory_banks.create()`, with an
  injected `agentplatform.Client` and with lazy default construction. Audit 96.0.
- google-cloud-aiplatform 2.4.0, on the older `vertexai` surface. Audit 96.0.

Single runs on single SDK versions, not a support guarantee. Transcripts are in
`RESULTS.md`.

## ABC implemented against (memory_hygiene_store.MemoryStore)

    class MemoryStore(abc.ABC):
        @abc.abstractmethod
        def all_records(self) -> List[MemoryRecord]: ...
        @abc.abstractmethod
        def update(self, record: MemoryRecord) -> None: ...
        @abc.abstractmethod
        def delete(self, record_id: str) -> None: ...
        # concrete helpers built on the three above:
        # add, add_many, get, live_records, __len__, __iter__

    @dataclass
    class MemoryRecord:
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

## Mismatches

1. Retrieval telemetry is missing. No per record retrieval counter, no last
   accessed timestamp. `retrieval_count` and `last_retrieved_at` are set to
   None, not 0. Consequence: orphan detection in the audit pass reports
   nothing on this backend instead of flagging every record.
2. No point in time retrieval API. There is no "as of" parameter on any read.
   Revisions DO keep every prior fact with a create_time and support rollback, so
   prior state is reconstructable client side, just not queryable directly. No
   validity interval. `valid_from` and `valid_to` are recoverable only for
   records this adapter wrote (round tripped through custom metadata).
   `live_records(past_datetime)` cannot be answered truthfully for the past.
3. No update primitive. The documented surface is create, retrieve, list,
   delete. `update()` is delete then create: not atomic, and the rewritten
   memory gets a new server side id.
4. No structure in a memory. One free text `fact` string per memory, so
   `entity`, `relation`, `value`, `fact_class`, `source_session` and
   `merged_from` ride in custom metadata. Memories produced by
   `generate_memories` carry none of that and fall back to a documented
   convention (whole fact becomes `value`, relation becomes "fact", entity
   derived from scope).
5. Scope is not usable as a list filter. `list_memories` filters with an EBNF
   filter string; scope equality is applied client side after listing.
6. Scope is capped and immutable (a small number of key value pairs), so it
   cannot carry arbitrary record metadata.
7. Timestamp formats differ. The API returns RFC 3339 with a Z suffix and up
   to nanosecond precision, which Python 3.9 `fromisoformat` rejects, and the
   audit pass compares against naive datetimes. The adapter normalises to
   naive UTC.
8. No originating session id on a listed memory, so `source_session` is only
   populated for records the adapter wrote.
9. Response shapes vary by surface: `retrieve_memories` wraps each hit as
   `{"memory": ..., "distance": ...}` while `list_memories` returns memories
   directly. The adapter normalises both.
