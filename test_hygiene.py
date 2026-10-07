"""Tests for memory-hygiene. Standard library ``unittest`` only.

Run from the repository root::

    python3 test_hygiene.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from typing import List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
# Works whether the tests sit in tests/ next to the memory_hygiene package or
# flat in the same directory as the published single-file modules.
for _candidate in (os.path.dirname(_HERE), _HERE):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

try:  # package layout
    from memory_hygiene.audit import (
        AuditConfig,
        audit_records,
        find_contradictions,
        find_duplicate_clusters,
        find_orphans,
        find_ttl_overdue,
        normalize_value,
        similarity,
    )
    from memory_hygiene.audit import main as audit_main
    from memory_hygiene.maintain import MaintenancePolicy, run_maintenance
    from memory_hygiene.maintain import main as maintain_main
    from memory_hygiene.store import (
        JsonMemoryStore,
        MemoryRecord,
        SqliteMemoryStore,
        utc_now,
    )
except ImportError:  # flat layout
    from memory_hygiene_audit import (
        AuditConfig,
        audit_records,
        find_contradictions,
        find_duplicate_clusters,
        find_orphans,
        find_ttl_overdue,
        normalize_value,
        similarity,
    )
    from memory_hygiene_audit import main as audit_main
    from memory_hygiene_maintain import MaintenancePolicy, run_maintenance
    from memory_hygiene_maintain import main as maintain_main
    from memory_hygiene_store import (
        JsonMemoryStore,
        MemoryRecord,
        SqliteMemoryStore,
        utc_now,
    )


NOW = datetime(2026, 3, 1, 12, 0, 0)
SCRATCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".scratch")


def record(
    record_id: str,
    entity: str,
    relation: str,
    value: str,
    written_days_ago: float,
    fact_class: str = "status",
    retrieval_count: int = 1,
    valid_to: Optional[datetime] = None,
) -> MemoryRecord:
    """Build a record written ``written_days_ago`` before :data:`NOW`."""
    written_at = NOW - timedelta(days=written_days_ago)
    return MemoryRecord(
        id=record_id,
        entity=entity,
        relation=relation,
        value=value,
        written_at=written_at,
        valid_to=valid_to,
        fact_class=fact_class,
        last_retrieved_at=written_at if retrieval_count else None,
        retrieval_count=retrieval_count,
        source_session="test",
    )


def make_store(records: List[MemoryRecord]) -> SqliteMemoryStore:
    """An in-memory SQLite store preloaded with ``records``."""
    store = SqliteMemoryStore(":memory:")
    store.add_many(records)
    return store


def default_policy(**kwargs) -> MaintenancePolicy:
    """A maintenance policy pinned to :data:`NOW`."""
    return MaintenancePolicy(now=NOW, **kwargs)


class TestNormalization(unittest.TestCase):
    """Value normalization and similarity behave as the docs claim."""

    def test_normalization_strips_case_and_punctuation(self) -> None:
        self.assertEqual(
            normalize_value("Subscription tier is GOLD."),
            normalize_value("subscription  tier is gold"),
        )

    def test_similarity_is_between_zero_and_one(self) -> None:
        score = similarity("tier is gold", "tier is silver")
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)


class TestContradictionDetection(unittest.TestCase):
    """Same key, different value, both open -> contradiction."""

    def test_detects_open_contradiction(self) -> None:
        records = [
            record("a", "u1", "tier", "subscription tier is starter", 40),
            record("b", "u1", "tier", "subscription tier is premium", 10),
        ]
        pairs = find_contradictions(records, 0.90)
        self.assertEqual(len(pairs), 1)
        self.assertEqual({pairs[0][0].id, pairs[0][1].id}, {"a", "b"})

    def test_closed_record_is_not_a_contradiction(self) -> None:
        closed = record(
            "a",
            "u1",
            "tier",
            "subscription tier is starter",
            40,
            valid_to=NOW - timedelta(days=10),
        )
        live = record("b", "u1", "tier", "subscription tier is premium", 10)
        config = AuditConfig(now=NOW)
        report = audit_records([closed, live], config)
        self.assertEqual(report.contradiction_count, 0)
        self.assertEqual(report.live_records, 1)
        self.assertEqual(report.closed_records, 1)

    def test_different_entities_do_not_contradict(self) -> None:
        records = [
            record("a", "u1", "tier", "subscription tier is starter", 5),
            record("b", "u2", "tier", "subscription tier is premium", 5),
        ]
        self.assertEqual(find_contradictions(records, 0.90), [])

    def test_near_duplicates_are_not_counted_as_contradictions(self) -> None:
        records = [
            record("a", "u1", "tier", "subscription tier is premium", 5),
            record("b", "u1", "tier", "subscription tier = premium", 4),
        ]
        self.assertEqual(find_contradictions(records, 0.90), [])
        self.assertEqual(len(find_duplicate_clusters(records, 0.90)), 1)

    def test_contradiction_drives_the_health_score_down(self) -> None:
        clean = [record("a", "u1", "tier", "subscription tier is premium", 5)]
        dirty = clean + [record("b", "u1", "tier", "subscription tier is starter", 4)]
        config = AuditConfig(now=NOW)
        self.assertGreater(
            audit_records(clean, config).health_score,
            audit_records(dirty, config).health_score,
        )


class TestSupersession(unittest.TestCase):
    """Closing superseded intervals leaves exactly one open belief."""

    def test_older_record_is_closed_at_the_newer_write_time(self) -> None:
        older = record("a", "u1", "tier", "subscription tier is starter", 40)
        newer = record("b", "u1", "tier", "subscription tier is premium", 10)
        store = make_store([older, newer])
        run_maintenance(store, default_policy(), dry_run=False)

        updated = {r.id: r for r in store.all_records()}
        self.assertEqual(updated["a"].valid_to, newer.written_at)
        self.assertIsNone(updated["b"].valid_to)
        self.assertFalse(updated["a"].is_believed_true(NOW))
        self.assertTrue(updated["b"].is_believed_true(NOW))

    def test_chain_of_three_leaves_one_open(self) -> None:
        records = [
            record("a", "u1", "tier", "subscription tier is starter", 40),
            record("b", "u1", "tier", "subscription tier is growth", 20),
            record("c", "u1", "tier", "subscription tier is premium", 5),
        ]
        store = make_store(records)
        run_maintenance(store, default_policy(), dry_run=False)
        open_ids = sorted(r.id for r in store.all_records() if r.is_believed_true(NOW))
        self.assertEqual(open_ids, ["c"])


class TestDuplicateMerging(unittest.TestCase):
    """Near-duplicates collapse into one record with the best fields."""

    def setUp(self) -> None:
        self.records = [
            record(
                "a",
                "u1",
                "lang",
                "preferred language is portuguese",
                30,
                fact_class="preference",
                retrieval_count=2,
            ),
            record(
                "b",
                "u1",
                "lang",
                "preferred language is portuguese.",
                10,
                fact_class="preference",
                retrieval_count=9,
            ),
        ]

    def test_merge_keeps_latest_written_at_under_a_stable_id(self) -> None:
        """The survivor keeps the earliest id but the LATEST write time.

        This assertion was inverted until the restatement-recency fix. Keeping
        the earliest ``written_at`` made a restated fact look older than a value
        it had already superseded, so the supersession step then closed the wrong
        record and the agent was left believing a stale value. The id stays
        stable so audit-log references survive a merge; only the recency moves.
        """
        store = make_store(self.records)
        run_maintenance(store, default_policy(), dry_run=False)
        remaining = store.all_records()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].id, "a")
        latest = max(r.written_at for r in self.records)
        self.assertEqual(remaining[0].written_at, latest)

    def test_merge_sums_retrieval_count_and_keeps_longest_value(self) -> None:
        """Retrieval counts add up across a merged cluster.

        Previously the survivor took the maximum count. That undercounts: a
        cluster is one fact asserted several times, so the number of times the
        fact was retrieved is the sum over its copies, not the largest single
        copy. Undercounting matters because the orphan metric keys on whether a
        record was ever retrieved, so a max rule can mark a genuinely used fact
        as an orphan and expire it.
        """
        store = make_store(self.records)
        run_maintenance(store, default_policy(), dry_run=False)
        winner = store.all_records()[0]
        expected = sum(r.retrieval_count for r in self.records)
        self.assertEqual(winner.retrieval_count, expected)
        self.assertEqual(winner.value, "preferred language is portuguese.")

    def test_merge_records_absorbed_ids(self) -> None:
        store = make_store(self.records)
        result = run_maintenance(store, default_policy(), dry_run=False)
        self.assertEqual(result.deleted_ids, ["b"])
        self.assertEqual(store.all_records()[0].merged_from, ["b"])
        actions = result.counts_by_action()
        self.assertEqual(actions.get("merge_duplicate"), 1)
        self.assertEqual(actions.get("absorb_duplicate"), 1)

    def test_distinct_values_are_never_merged(self) -> None:
        records = [
            record("a", "u1", "lang", "preferred language is german", 30, "preference"),
            record("b", "u1", "lang", "preferred language is japanese", 10, "preference"),
        ]
        store = make_store(records)
        run_maintenance(store, default_policy(), dry_run=False)
        self.assertEqual(len(store.all_records()), 2)


class TestTtlExpiry(unittest.TestCase):
    """Per-class TTL closes overdue records and only those."""

    def test_overdue_ephemeral_is_closed_at_its_expiry(self) -> None:
        stale = record("a", "u1", "incident", "active incident is backlog", 30, "ephemeral")
        store = make_store([stale])
        run_maintenance(store, default_policy(), dry_run=False)
        updated = store.all_records()[0]
        self.assertEqual(updated.valid_to, stale.written_at + timedelta(days=7))
        self.assertFalse(updated.is_believed_true(NOW))

    def test_fresh_ephemeral_is_left_open(self) -> None:
        fresh = record("a", "u1", "incident", "active incident is backlog", 2, "ephemeral")
        store = make_store([fresh])
        run_maintenance(store, default_policy(), dry_run=False)
        self.assertIsNone(store.all_records()[0].valid_to)

    def test_class_without_ttl_is_never_closed(self) -> None:
        forever = record("a", "u1", "region", "home region is eastern asia", 900, "identity")
        store = make_store([forever])
        run_maintenance(store, default_policy(), dry_run=False)
        self.assertIsNone(store.all_records()[0].valid_to)

    def test_custom_ttl_policy_is_honoured(self) -> None:
        pref = record("a", "u1", "lang", "preferred language is german", 45, "preference")
        store = make_store([pref])
        policy = default_policy(ttl_policy={"preference": timedelta(days=30)})
        run_maintenance(store, policy, dry_run=False)
        self.assertEqual(
            store.all_records()[0].valid_to, pref.written_at + timedelta(days=30)
        )

    def test_audit_flags_overdue_records(self) -> None:
        stale = record("a", "u1", "incident", "active incident is backlog", 30, "ephemeral")
        overdue = find_ttl_overdue([stale], AuditConfig(now=NOW))
        self.assertEqual([r.id for r in overdue], ["a"])


class TestOrphans(unittest.TestCase):
    """Orphan detection respects the grace period."""

    def test_never_retrieved_and_old_is_an_orphan(self) -> None:
        old = record("a", "u1", "tier", "subscription tier is growth", 40, retrieval_count=0)
        self.assertEqual([r.id for r in find_orphans([old], NOW, 14.0)], ["a"])

    def test_recent_unread_record_is_not_yet_an_orphan(self) -> None:
        recent = record("a", "u1", "tier", "subscription tier is growth", 3, retrieval_count=0)
        self.assertEqual(find_orphans([recent], NOW, 14.0), [])

    def test_retrieved_record_is_not_an_orphan(self) -> None:
        read = record("a", "u1", "tier", "subscription tier is growth", 40, retrieval_count=4)
        self.assertEqual(find_orphans([read], NOW, 14.0), [])


class TestIdempotency(unittest.TestCase):
    """A second pass over a maintained store changes nothing."""

    def _messy_records(self) -> List[MemoryRecord]:
        return [
            record("a", "u1", "tier", "subscription tier is starter", 40),
            record("b", "u1", "tier", "subscription tier is premium", 10),
            record("c", "u2", "lang", "preferred language is german", 30, "preference"),
            record("d", "u2", "lang", "preferred language is german.", 12, "preference"),
            record("e", "u3", "incident", "active incident is backlog", 25, "ephemeral"),
            record("f", "u4", "region", "home region is eastern asia", 60, "identity"),
        ]

    def test_second_pass_makes_no_changes(self) -> None:
        store = make_store(self._messy_records())
        first = run_maintenance(store, default_policy(), dry_run=False)
        self.assertGreater(first.change_count, 0)
        second = run_maintenance(store, default_policy(), dry_run=False)
        self.assertEqual(second.change_count, 0)
        self.assertEqual(second.deleted_ids, [])

    def test_store_state_is_byte_identical_after_the_second_pass(self) -> None:
        store = make_store(self._messy_records())
        run_maintenance(store, default_policy(), dry_run=False)
        snapshot = json.dumps([r.to_dict() for r in store.all_records()], sort_keys=True)
        run_maintenance(store, default_policy(), dry_run=False)
        self.assertEqual(
            snapshot,
            json.dumps([r.to_dict() for r in store.all_records()], sort_keys=True),
        )

    def test_health_score_is_stable_after_the_second_pass(self) -> None:
        store = make_store(self._messy_records())
        first = run_maintenance(store, default_policy(), dry_run=False)
        second = run_maintenance(store, default_policy(), dry_run=False)
        self.assertEqual(
            first.audit_after.health_score, second.audit_after.health_score
        )

    def test_maintenance_improves_the_health_score(self) -> None:
        store = make_store(self._messy_records())
        result = run_maintenance(store, default_policy(), dry_run=False)
        self.assertGreater(
            result.audit_after.health_score, result.audit_before.health_score
        )


class TestDryRun(unittest.TestCase):
    """A dry run reports everything and mutates nothing."""

    def setUp(self) -> None:
        os.makedirs(SCRATCH, exist_ok=True)
        self.records = [
            record("a", "u1", "tier", "subscription tier is starter", 40),
            record("b", "u1", "tier", "subscription tier is premium", 10),
            record("c", "u2", "lang", "preferred language is german", 30, "preference"),
            record("d", "u2", "lang", "preferred language is german.", 12, "preference"),
            record("e", "u3", "incident", "active incident is backlog", 25, "ephemeral"),
        ]

    def test_dry_run_is_the_default(self) -> None:
        store = make_store(self.records)
        result = run_maintenance(store, default_policy())
        self.assertTrue(result.dry_run)

    def test_dry_run_leaves_the_store_untouched(self) -> None:
        store = make_store(self.records)
        before = json.dumps([r.to_dict() for r in store.all_records()], sort_keys=True)
        result = run_maintenance(store, default_policy(), dry_run=True)
        after = json.dumps([r.to_dict() for r in store.all_records()], sort_keys=True)
        self.assertGreater(result.change_count, 0)
        self.assertEqual(before, after)

    def test_dry_run_leaves_the_backing_file_untouched(self) -> None:
        path = os.path.join(SCRATCH, "store.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "records": []}, handle)
        store = JsonMemoryStore(path)
        store.add_many(self.records)
        with open(path, "r", encoding="utf-8") as handle:
            before = handle.read()
        run_maintenance(store, default_policy(), dry_run=True)
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(before, handle.read())

    def test_dry_run_writes_no_log(self) -> None:
        log_path = os.path.join(SCRATCH, "maintenance.jsonl")
        store = make_store(self.records)
        run_maintenance(store, default_policy(), dry_run=True, log_path=log_path)
        self.assertFalse(os.path.exists(log_path))

    def test_dry_run_and_apply_plan_the_same_changes(self) -> None:
        planned = run_maintenance(make_store(self.records), default_policy())
        applied = run_maintenance(
            make_store(self.records), default_policy(), dry_run=False
        )
        self.assertEqual(planned.change_count, applied.change_count)
        self.assertEqual(planned.counts_by_action(), applied.counts_by_action())
        self.assertEqual(
            planned.audit_after.health_score, applied.audit_after.health_score
        )


class TestMaintenanceLog(unittest.TestCase):
    """The JSONL log records every applied change."""

    def setUp(self) -> None:
        os.makedirs(SCRATCH, exist_ok=True)
        self.log_path = os.path.join(SCRATCH, "applied.jsonl")
        if os.path.exists(self.log_path):
            with open(self.log_path, "w", encoding="utf-8") as handle:
                handle.write("")

    def test_log_has_one_line_per_change_with_before_and_after(self) -> None:
        store = make_store(
            [
                record("a", "u1", "tier", "subscription tier is starter", 40),
                record("b", "u1", "tier", "subscription tier is premium", 10),
            ]
        )
        result = run_maintenance(
            store, default_policy(), dry_run=False, log_path=self.log_path
        )
        with open(self.log_path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
        self.assertEqual(len(lines), result.change_count)
        entry = json.loads(lines[0])
        for key in ("ran_at", "action", "record_id", "before", "after", "reason"):
            self.assertIn(key, entry)


class TestStoreBackends(unittest.TestCase):
    """Both reference backends round-trip records faithfully."""

    def test_sqlite_round_trip(self) -> None:
        original = record("a", "u1", "tier", "subscription tier is premium", 5)
        store = make_store([original])
        loaded = store.all_records()[0]
        self.assertEqual(loaded.to_dict(), original.to_dict())

    def test_json_round_trip_through_disk(self) -> None:
        os.makedirs(SCRATCH, exist_ok=True)
        path = os.path.join(SCRATCH, "round_trip.json")
        original = record("a", "u1", "tier", "subscription tier is premium", 5)
        JsonMemoryStore(path).add(original)
        loaded = JsonMemoryStore(path).all_records()[0]
        self.assertEqual(loaded.to_dict(), original.to_dict())

    def test_delete_removes_the_record(self) -> None:
        store = make_store([record("a", "u1", "tier", "t", 1)])
        store.delete("a")
        self.assertEqual(store.all_records(), [])

    def test_deleting_a_missing_id_is_not_an_error(self) -> None:
        store = make_store([])
        store.delete("nope")
        self.assertEqual(store.all_records(), [])


class TestSimulation(unittest.TestCase):
    """The published simulation is reproducible and its claims hold."""

    def test_event_stream_is_deterministic_for_a_fixed_seed(self) -> None:
        import demo_drift

        first = demo_drift.generate_events(demo_drift.SEED)
        second = demo_drift.generate_events(demo_drift.SEED)
        self.assertEqual(
            [r.to_dict() for day in sorted(first.writes) for r in first.writes[day]],
            [r.to_dict() for day in sorted(second.writes) for r in second.writes[day]],
        )
        self.assertEqual(first.probes, second.probes)

    def test_generated_duplicates_are_detectable_as_duplicates(self) -> None:
        import demo_drift

        rng = __import__("random").Random(7)
        for value in ["subscription tier is premium", "preferred language is german"]:
            for _ in range(20):
                variant = demo_drift._reword(value, rng)
                self.assertGreaterEqual(
                    similarity(variant, value), demo_drift.SIMILARITY_THRESHOLD
                )


class TestRestatementRecency(unittest.TestCase):
    """Merging a restated fact must not throw away its recency.

    Regression cover for the bug where ``_merge_duplicates`` collapsed a
    cluster into its earliest copy. The survivor then looked older than a
    competing value that had in fact been superseded, so the later
    supersession and TTL steps both reached the wrong conclusion.
    """

    JAN = 2026, 1

    def _at(self, day: int) -> datetime:
        return datetime(self.JAN[0], self.JAN[1], day)

    def _rec(
        self,
        record_id: str,
        value: str,
        day: int,
        fact_class: str = "status",
        retrieval_count: int = 1,
    ) -> MemoryRecord:
        written = self._at(day)
        return MemoryRecord(
            id=record_id,
            entity="u1",
            relation="home_city",
            value=value,
            written_at=written,
            fact_class=fact_class,
            last_retrieved_at=written if retrieval_count else None,
            retrieval_count=retrieval_count,
            source_session="test",
        )

    def _believed(self, store, now: datetime) -> List[MemoryRecord]:
        return [r for r in store.all_records() if r.is_believed_true(now)]

    def test_restated_value_wins_over_the_value_it_replaced(self) -> None:
        """Berlin, then Munich, then Berlin again: Berlin is current."""
        now = self._at(20)
        store = make_store(
            [
                self._rec("r1", "Berlin", 1),
                self._rec("r2", "Munich", 5),
                self._rec("r3", "Berlin", 10),
            ]
        )
        run_maintenance(store, MaintenancePolicy(now=now), dry_run=False)
        believed = self._believed(store, now)
        self.assertEqual([r.value for r in believed], ["Berlin"])
        # The Munich record must be closed at the moment Berlin was restated.
        munich = [r for r in store.all_records() if r.value == "Munich"]
        self.assertEqual(len(munich), 1)
        self.assertEqual(munich[0].valid_to, self._at(10))

    def test_survivor_carries_the_latest_write_time_and_a_stable_id(self) -> None:
        now = self._at(20)
        store = make_store(
            [
                self._rec("r1", "Berlin", 1),
                self._rec("r2", "Munich", 5),
                self._rec("r3", "Berlin", 10),
            ]
        )
        run_maintenance(store, MaintenancePolicy(now=now), dry_run=False)
        survivor = [r for r in store.all_records() if r.value == "Berlin"][0]
        # Earliest id is kept so references and the audit log stay stable.
        self.assertEqual(survivor.id, "r1")
        self.assertEqual(survivor.written_at, self._at(10))
        self.assertEqual(survivor.valid_from, self._at(10))
        self.assertIsNone(survivor.valid_to)
        self.assertEqual(survivor.merged_from, ["r3"])
        # retrieval_count is the sum across the cluster, not the max.
        self.assertEqual(survivor.retrieval_count, 2)
        self.assertEqual(survivor.last_retrieved_at, self._at(10))

    def test_restatement_listed_first_still_resolves_by_write_time(self) -> None:
        """Same facts, reversed list order. The newest write still wins.

        Guards against fixing the bug by simply inverting it, for example by
        picking the last element of the cluster list instead of comparing
        ``written_at``.
        """
        now = self._at(20)
        store = make_store(
            [
                self._rec("r3", "Berlin", 10),
                self._rec("r2", "Munich", 5),
                self._rec("r1", "Berlin", 1),
            ]
        )
        run_maintenance(store, MaintenancePolicy(now=now), dry_run=False)
        believed = self._believed(store, now)
        self.assertEqual([r.value for r in believed], ["Berlin"])
        survivor = believed[0]
        self.assertEqual(survivor.written_at, self._at(10))

    def test_recently_restated_ephemeral_is_not_expired_against_the_old_copy(
        self,
    ) -> None:
        """TTL must be measured from the restatement, not the first write."""
        now = self._at(20)
        store = make_store(
            [
                self._rec("e1", "oncall rotation is team blue", 1, "ephemeral"),
                self._rec("e2", "oncall rotation is team blue", 18, "ephemeral"),
            ]
        )
        policy = MaintenancePolicy(
            now=now, ttl_policy={"ephemeral": timedelta(days=10)}
        )
        run_maintenance(store, policy, dry_run=False)
        remaining = store.all_records()
        self.assertEqual(len(remaining), 1)
        survivor = remaining[0]
        self.assertEqual(survivor.written_at, self._at(18))
        # Written 2 days ago under a 10 day TTL, so it is still live.
        self.assertIsNone(survivor.valid_to)
        self.assertTrue(survivor.is_believed_true(now))

    def test_three_distinct_values_supersede_normally(self) -> None:
        """No duplicates at all. Plain supersession is untouched by the fix."""
        now = self._at(20)
        store = make_store(
            [
                self._rec("d1", "Berlin", 1),
                self._rec("d2", "Munich", 5),
                self._rec("d3", "Lisbon", 10),
            ]
        )
        run_maintenance(store, MaintenancePolicy(now=now), dry_run=False)
        remaining = {r.id: r for r in store.all_records()}
        # Nothing was merged away.
        self.assertEqual(sorted(remaining), ["d1", "d2", "d3"])
        self.assertEqual(remaining["d1"].valid_to, self._at(5))
        self.assertEqual(remaining["d2"].valid_to, self._at(10))
        self.assertIsNone(remaining["d3"].valid_to)
        believed = self._believed(store, now)
        self.assertEqual([r.value for r in believed], ["Lisbon"])

    def test_merge_does_not_shorten_a_still_open_belief(self) -> None:
        """A closed copy must not drag an open restatement shut.

        ``valid_to`` takes the latest end across the cluster, and an open
        record (``None``) is the latest end there is.
        """
        now = self._at(20)
        early = self._rec("c1", "Berlin", 1)
        early.valid_to = self._at(25)
        late = self._rec("c2", "Berlin", 10)
        store = make_store([early, late])
        run_maintenance(store, MaintenancePolicy(now=now), dry_run=False)
        survivor = store.all_records()[0]
        self.assertEqual(survivor.id, "c1")
        self.assertIsNone(survivor.valid_to)
        self.assertTrue(survivor.is_believed_true(now))


class TestTimezoneAwareClock(unittest.TestCase):
    """A store with timezone-aware timestamps must not blow up on compare."""

    def _aware_records(self) -> List[MemoryRecord]:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [
            MemoryRecord(
                id="z1",
                entity="u1",
                relation="home_city",
                value="Berlin",
                written_at=base,
                fact_class="status",
                last_retrieved_at=base,
                retrieval_count=1,
                source_session="test",
            ),
            MemoryRecord(
                id="z2",
                entity="u1",
                relation="home_city",
                value="Munich",
                written_at=base + timedelta(days=4),
                fact_class="status",
                last_retrieved_at=base + timedelta(days=4),
                retrieval_count=1,
                source_session="test",
            ),
        ]

    def test_default_clock_is_timezone_aware_utc(self) -> None:
        moment = utc_now()
        self.assertIsNotNone(moment.tzinfo)
        self.assertEqual(moment.utcoffset(), timedelta(0))

    def test_naive_clock_against_an_aware_store_does_not_raise(self) -> None:
        store = make_store(self._aware_records())
        result = run_maintenance(
            store, MaintenancePolicy(now=datetime(2026, 1, 20)), dry_run=False
        )
        self.assertGreaterEqual(result.change_count, 1)
        live = [
            r
            for r in store.all_records()
            if r.is_believed_true(datetime(2026, 1, 20, tzinfo=timezone.utc))
        ]
        self.assertEqual([r.value for r in live], ["Munich"])

    def test_aware_clock_against_a_naive_store_does_not_raise(self) -> None:
        store = make_store(
            [
                record("n1", "u1", "home_city", "Berlin", 20),
                record("n2", "u1", "home_city", "Munich", 10),
            ]
        )
        aware_now = NOW.replace(tzinfo=timezone.utc)
        result = run_maintenance(store, MaintenancePolicy(now=aware_now), dry_run=False)
        self.assertGreaterEqual(result.change_count, 1)
        live = [r for r in store.all_records() if r.is_believed_true(NOW)]
        self.assertEqual([r.value for r in live], ["Munich"])

    def test_audit_of_an_aware_store_with_a_naive_clock_does_not_raise(self) -> None:
        report = audit_records(
            self._aware_records(), AuditConfig(now=datetime(2026, 1, 20))
        )
        self.assertEqual(report.live_records, 2)

    def test_cli_default_clock_handles_an_aware_store(self) -> None:
        os.makedirs(SCRATCH, exist_ok=True)
        path = os.path.join(SCRATCH, "aware_store_c7d1.json")
        store = JsonMemoryStore(path)
        store.add_many(self._aware_records())
        self.assertEqual(maintain_main(["--store", path, "--dry-run"]), 0)
        self.assertEqual(audit_main(["--store", path]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
