#!/usr/bin/env python3
"""Seeded, reproducible simulation of agent memory drift over 30 days.

The simulation generates one event stream (writes, supersessions, duplicate
restatements, retrievals) from a fixed seed, then replays that *identical*
stream against two arms:

* ``no-maintenance`` - nothing is ever invalidated, the usual default.
* ``maintenance``    - the hygiene pass runs every N simulated days.

Every day, after the day's events, both arms are probed with the same
question for every known ``(entity, relation)`` key: "what is currently true
here?" A naive retriever returns all records still believed true for that key
and picks among them with no extra signal, so the day's score for a probe is
``matching_candidates / candidates`` - the expected hit rate of a retriever
that cannot tell a live fact from a dead one. When the ground truth has
expired, the correct answer is "nothing", and only an empty candidate set
scores 1.0.

There is no wall-clock or filesystem state in the output, so two runs produce
byte-identical stdout. The seed is printed at the top of every run.
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:  # package layout: memory_hygiene/ next to this file
    from memory_hygiene.audit import (  # noqa: E402
        DEFAULT_TTL_POLICY,
        AuditConfig,
        audit_store,
        normalize_value,
        similarity,
    )
    from memory_hygiene.maintain import MaintenancePolicy, run_maintenance  # noqa: E402
    from memory_hygiene.store import JsonMemoryStore, MemoryRecord  # noqa: E402
except ImportError:  # flat layout: memory_hygiene_*.py next to this file
    from memory_hygiene_audit import (  # noqa: E402
        DEFAULT_TTL_POLICY,
        AuditConfig,
        audit_store,
        normalize_value,
        similarity,
    )
    from memory_hygiene_maintain import MaintenancePolicy, run_maintenance  # noqa: E402
    from memory_hygiene_store import JsonMemoryStore, MemoryRecord  # noqa: E402

# ---------------------------------------------------------------------------
# Simulation configuration. Everything below is fixed; change the seed only if
# you want a different world, not a different answer.
# ---------------------------------------------------------------------------

SEED = 20260930
SIM_START = datetime(2026, 1, 1)
DAYS = 30
N_ENTITIES = 40
MAINTENANCE_EVERY_DAYS = 7
SIMILARITY_THRESHOLD = 0.90
EPHEMERAL_TRUTH_DAYS = 7
BOOTSTRAP_FACTS = 48
OUTPUT_DIR = ".demo_runs"

#: TTL policy used by both the audit and the maintenance arm.
TTL_POLICY: Dict[str, Optional[timedelta]] = dict(DEFAULT_TTL_POLICY)

#: Relations and candidate values per fact class.
VOCABULARY: Dict[str, Dict[str, List[str]]] = {
    "identity": {
        "home_region": [
            "home region is northern europe",
            "home region is southern europe",
            "home region is eastern asia",
            "home region is western america",
        ],
        "account_type": [
            "account type is individual seat",
            "account type is team seat",
            "account type is enterprise seat",
        ],
    },
    "preference": {
        "preferred_language": [
            "preferred language is english",
            "preferred language is german",
            "preferred language is japanese",
            "preferred language is portuguese",
        ],
        "notification_channel": [
            "notification channel is email digest",
            "notification channel is in product only",
            "notification channel is weekly summary",
        ],
        "report_format": [
            "report format is compact table",
            "report format is long narrative",
            "report format is bullet summary",
        ],
    },
    "status": {
        "subscription_tier": [
            "subscription tier is starter",
            "subscription tier is growth",
            "subscription tier is premium",
        ],
        "assigned_team": [
            "assigned team is platform group",
            "assigned team is data group",
            "assigned team is support group",
        ],
        "primary_workspace": [
            "primary workspace is research area",
            "primary workspace is billing area",
            "primary workspace is onboarding area",
        ],
    },
    "ephemeral": {
        "active_incident": [
            "active incident is queue backlog",
            "active incident is login latency",
            "active incident is export failure",
        ],
        "on_call_shift": [
            "on call shift is morning rotation",
            "on call shift is evening rotation",
        ],
    },
}

CLASS_WEIGHTS: List[Tuple[str, int]] = [
    ("identity", 15),
    ("preference", 35),
    ("status", 35),
    ("ephemeral", 15),
]


# ---------------------------------------------------------------------------
# Event stream generation (runs once, shared by both arms)
# ---------------------------------------------------------------------------


def _weighted_class(rng: random.Random) -> str:
    """Pick a fact class using CLASS_WEIGHTS."""
    total = sum(weight for _, weight in CLASS_WEIGHTS)
    draw = rng.randrange(total)
    upto = 0
    for name, weight in CLASS_WEIGHTS:
        upto += weight
        if draw < upto:
            return name
    return CLASS_WEIGHTS[-1][0]


def _reword(value: str, rng: random.Random) -> str:
    """Return a restatement of ``value`` that is a near-duplicate, not a change.

    The variant is checked against :data:`SIMILARITY_THRESHOLD` and falls back
    to a punctuation-only change if the chosen edit drifted too far, so a
    duplicate is always detectable as a duplicate.
    """
    candidates = [
        value.replace(" is ", " = "),
        value.replace(" is ", " -> "),
        value + ".",
        value.capitalize() + " .",
        value.replace(" ", "  "),
    ]
    variant = candidates[rng.randrange(len(candidates))]
    if variant == value or similarity(variant, value) < SIMILARITY_THRESHOLD:
        variant = value + "."
    return variant


def _other_value(fact_class: str, relation: str, current: str, rng: random.Random) -> str:
    """Pick a value for ``relation`` that genuinely differs from ``current``."""
    options = [
        v
        for v in VOCABULARY[fact_class][relation]
        if normalize_value(v) != normalize_value(current)
    ]
    return options[rng.randrange(len(options))]


class EventStream:
    """The generated world: per-day write/retrieve events and probe answers."""

    def __init__(self) -> None:
        self.writes: Dict[int, List[MemoryRecord]] = {}
        self.retrievals: Dict[int, List[str]] = {}
        self.probes: Dict[int, List[Tuple[str, Optional[str]]]] = {}
        self.total_writes = 0
        self.supersessions = 0
        self.duplicate_writes = 0


def _timestamp(day: int, hour: int) -> datetime:
    """Simulated clock: day 1 is SIM_START."""
    return SIM_START + timedelta(days=day - 1, hours=hour)


def generate_events(seed: int) -> EventStream:
    """Build the full 30-day event stream and the per-day ground truth.

    Returns:
        An :class:`EventStream`. ``probes[day]`` maps every known key to the
        value that is actually true at the end of that day, or ``None`` when
        nothing should be believed any more.
    """
    rng = random.Random(seed)
    stream = EventStream()
    entities = ["user:%03d" % (i + 1) for i in range(N_ENTITIES)]

    # key -> (value, fact_class, day the value became true)
    truth: Dict[str, Tuple[str, str, int]] = {}
    # key -> (entity, relation, fact_class)
    key_meta: Dict[str, Tuple[str, str, str]] = {}
    counter = [0]

    def new_id() -> str:
        counter[0] += 1
        return "m%04d" % counter[0]

    def write(
        entity: str,
        relation: str,
        fact_class: str,
        value: str,
        day: int,
        session: str,
    ) -> None:
        record = MemoryRecord(
            id=new_id(),
            entity=entity,
            relation=relation,
            value=value,
            written_at=_timestamp(day, 9),
            fact_class=fact_class,
            source_session=session,
        )
        stream.writes.setdefault(day, []).append(record)
        stream.total_writes += 1

    def fresh_key(day: int) -> None:
        """Write a fact for a key that does not exist yet."""
        for _ in range(40):
            entity = entities[rng.randrange(len(entities))]
            fact_class = _weighted_class(rng)
            relations = sorted(VOCABULARY[fact_class])
            relation = relations[rng.randrange(len(relations))]
            key = "%s|%s" % (entity, relation)
            if key in truth:
                continue
            values = VOCABULARY[fact_class][relation]
            value = values[rng.randrange(len(values))]
            write(entity, relation, fact_class, value, day, "s%03d" % day)
            truth[key] = (value, fact_class, day)
            key_meta[key] = (entity, relation, fact_class)
            return

    for day in range(1, DAYS + 1):
        # 1. new facts
        n_new = BOOTSTRAP_FACTS if day == 1 else rng.randint(2, 5)
        for _ in range(n_new):
            fresh_key(day)

        # 2. supersessions: a new value is written, the old record is left open
        if day >= 3:
            candidates = sorted(k for k, meta in key_meta.items() if meta[2] != "identity")
            for _ in range(rng.randint(2, 5)):
                if not candidates:
                    break
                key = candidates[rng.randrange(len(candidates))]
                entity, relation, fact_class = key_meta[key]
                current = truth[key][0]
                value = _other_value(fact_class, relation, current, rng)
                write(entity, relation, fact_class, value, day, "s%03d" % day)
                truth[key] = (value, fact_class, day)
                stream.supersessions += 1

        # 3. duplicate restatements from a repeated session
        if day >= 2:
            candidates = sorted(key_meta)
            for _ in range(rng.randint(1, 3)):
                if not candidates:
                    break
                key = candidates[rng.randrange(len(candidates))]
                entity, relation, fact_class = key_meta[key]
                current = truth[key][0]
                write(
                    entity,
                    relation,
                    fact_class,
                    _reword(current, rng),
                    day,
                    "s%03dr" % day,
                )
                stream.duplicate_writes += 1

        # 4. which keys the agent actually reads today
        known = sorted(key_meta)
        n_reads = min(len(known), rng.randint(8, 16))
        stream.retrievals[day] = sorted(rng.sample(known, n_reads))

        # 5. ground truth for the end of the day, for every known key
        answers: List[Tuple[str, Optional[str]]] = []
        for key in known:
            value, fact_class, set_day = truth[key]
            if fact_class == "ephemeral" and (day - set_day) >= EPHEMERAL_TRUTH_DAYS:
                answers.append((key, None))
            else:
                answers.append((key, value))
        stream.probes[day] = answers

    return stream


# ---------------------------------------------------------------------------
# Retrieval probe
# ---------------------------------------------------------------------------


def candidates_for(
    records: Sequence[MemoryRecord], key: str, now: datetime
) -> List[MemoryRecord]:
    """Records still believed true for ``entity|relation`` at ``now``."""
    return [r for r in records if r.key == key and r.is_believed_true(now)]


def value_matches(candidate: str, expected: str) -> bool:
    """True when a stored value is the expected fact, allowing rewordings."""
    if normalize_value(candidate) == normalize_value(expected):
        return True
    return similarity(candidate, expected) >= SIMILARITY_THRESHOLD


def probe_accuracy(
    records: Sequence[MemoryRecord],
    answers: Sequence[Tuple[str, Optional[str]]],
    now: datetime,
) -> float:
    """Mean expected hit rate over every probe of the day, in [0, 1]."""
    if not answers:
        return 1.0
    by_key: Dict[str, List[MemoryRecord]] = {}
    for record in records:
        if record.is_believed_true(now):
            by_key.setdefault(record.key, []).append(record)

    total = 0.0
    for key, expected in answers:
        found = by_key.get(key, [])
        if expected is None:
            total += 1.0 if not found else 0.0
            continue
        if not found:
            continue
        hits = sum(1 for r in found if value_matches(r.value, expected))
        total += hits / len(found)
    return total / len(answers)


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


class DayResult:
    """One day's measurement for one arm."""

    def __init__(
        self,
        day: int,
        accuracy: float,
        live_records: int,
        total_records: int,
        maintained: bool,
        changes: int,
    ) -> None:
        self.day = day
        self.accuracy = accuracy
        self.live_records = live_records
        self.total_records = total_records
        self.maintained = maintained
        self.changes = changes


def run_arm(
    name: str,
    stream: EventStream,
    maintain_every: Optional[int],
    store_path: str,
    log_path: Optional[str],
) -> Tuple[List[DayResult], JsonMemoryStore, int]:
    """Replay the event stream against one arm.

    Args:
        name: Arm label, used only for the store filename.
        stream: The shared event stream.
        maintain_every: Run the hygiene pass every N days, or ``None`` never.
        store_path: Where to persist the final store for inspection.
        log_path: JSONL maintenance log path, or ``None``.

    Returns:
        ``(per-day results, the store, total maintenance changes)``.
    """
    store = JsonMemoryStore(store_path, write_through=False)
    store._records.clear()  # start from a clean world even if the file existed
    results: List[DayResult] = []
    total_changes = 0

    for day in range(1, DAYS + 1):
        for record in stream.writes.get(day, []):
            store.add(record.copy())

        # retrieval bookkeeping: everything believed true for a read key is
        # returned, so all of it counts as retrieved.
        read_time = _timestamp(day, 12)
        all_records = store.all_records()
        for key in stream.retrievals.get(day, []):
            for record in candidates_for(all_records, key, read_time):
                record.retrieval_count += 1
                record.last_retrieved_at = read_time
                store.update(record)

        maintained = False
        changes = 0
        if maintain_every and day % maintain_every == 0:
            policy = MaintenancePolicy(
                now=_timestamp(day, 18),
                ttl_policy=dict(TTL_POLICY),
                duplicate_threshold=SIMILARITY_THRESHOLD,
            )
            result = run_maintenance(store, policy, dry_run=False, log_path=log_path)
            maintained = True
            changes = result.change_count
            total_changes += changes

        probe_time = _timestamp(day, 20)
        records = store.all_records()
        accuracy = probe_accuracy(records, stream.probes[day], probe_time)
        live = sum(1 for r in records if r.is_believed_true(probe_time))
        results.append(
            DayResult(day, accuracy, live, len(records), maintained, changes)
        )

    store.save()
    return results, store, total_changes


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def main() -> int:
    """Run both arms and print the day-by-day table plus the headline deltas."""
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_DIR)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "maintenance_arm_b.jsonl")
    if os.path.exists(log_path):
        # keep the log for this run only, so repeated runs stay comparable
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("")

    stream = generate_events(SEED)

    arm_a, store_a, _ = run_arm(
        "no_maintenance", stream, None, os.path.join(out_dir, "store_no_maintenance.json"), None
    )
    arm_b, store_b, changes_b = run_arm(
        "maintenance",
        stream,
        MAINTENANCE_EVERY_DAYS,
        os.path.join(out_dir, "store_maintenance.json"),
        log_path,
    )

    print("=" * 78)
    print("AGENT MEMORY DRIFT SIMULATION")
    print("=" * 78)
    print("seed                    : %d" % SEED)
    print("simulated days          : %d  (day 1 = %s)" % (DAYS, SIM_START.date().isoformat()))
    print("entities                : %d" % N_ENTITIES)
    print("fact classes            : identity, preference, status, ephemeral")
    print("similarity threshold    : %.2f" % SIMILARITY_THRESHOLD)
    print(
        "ttl policy (days)       : %s"
        % ", ".join(
            "%s=%s" % (name, "none" if ttl is None else "%g" % (ttl.total_seconds() / 86400.0))
            for name, ttl in sorted(TTL_POLICY.items())
        )
    )
    print("maintenance arm cadence : every %d days" % MAINTENANCE_EVERY_DAYS)
    print("")
    print("event stream (identical for both arms)")
    print("  writes total          : %d" % stream.total_writes)
    print("  of which supersessions: %d" % stream.supersessions)
    print("  of which duplicates   : %d" % stream.duplicate_writes)
    print("  tracked keys          : %d" % len(stream.probes[DAYS]))
    print("")
    print("probe: every known (entity, relation) key is asked 'what is true now?'")
    print("       score per probe = matching candidates / candidates believed true")
    print("       (expired ground truth: 1.0 only if nothing is returned)")
    print("")
    print("-" * 78)
    print(
        "%4s | %6s %7s | %6s %7s %4s | %8s"
        % ("day", "recs", "acc", "recs", "acc", "mnt", "delta")
    )
    print(
        "%4s | %6s %7s | %6s %7s %4s | %8s"
        % ("", "(A)", "(A)", "(B)", "(B)", "", "B - A")
    )
    print("-" * 78)
    for a, b in zip(arm_a, arm_b):
        print(
            "%4d | %6d %6.1f%% | %6d %6.1f%% %4s | %+7.1fpp"
            % (
                a.day,
                a.live_records,
                100.0 * a.accuracy,
                b.live_records,
                100.0 * b.accuracy,
                "yes" if b.maintained else "",
                100.0 * (b.accuracy - a.accuracy),
            )
        )
    print("-" * 78)
    print("")

    config = AuditConfig(
        now=_timestamp(DAYS, 20),
        duplicate_threshold=SIMILARITY_THRESHOLD,
        orphan_grace_days=14.0,
        ttl_policy=dict(TTL_POLICY),
    )
    audit_a = audit_store(store_a, config)
    audit_b = audit_store(store_b, config)

    print("=" * 78)
    print("HEADLINE NUMBERS")
    print("=" * 78)
    print("%-34s %14s %14s" % ("", "no maintenance", "maintenance"))
    print(
        "%-34s %13.1f%% %13.1f%%"
        % ("day 1 retrieval accuracy", 100.0 * arm_a[0].accuracy, 100.0 * arm_b[0].accuracy)
    )
    print(
        "%-34s %13.1f%% %13.1f%%"
        % ("day 30 retrieval accuracy", 100.0 * arm_a[-1].accuracy, 100.0 * arm_b[-1].accuracy)
    )
    print(
        "%-34s %13.1fpp %13.1fpp"
        % (
            "accuracy change day 1 -> day 30",
            100.0 * (arm_a[-1].accuracy - arm_a[0].accuracy),
            100.0 * (arm_b[-1].accuracy - arm_b[0].accuracy),
        )
    )
    print(
        "%-34s %14.1f %14.1f"
        % ("day 30 memory health score", audit_a.health_score, audit_b.health_score)
    )
    print(
        "%-34s %14d %14d"
        % ("day 30 records stored", audit_a.total_records, audit_b.total_records)
    )
    print(
        "%-34s %14d %14d"
        % ("day 30 records believed true", audit_a.live_records, audit_b.live_records)
    )
    print(
        "%-34s %14d %14d"
        % (
            "day 30 contradiction pairs",
            audit_a.contradiction_count,
            audit_b.contradiction_count,
        )
    )
    print(
        "%-34s %14d %14d"
        % (
            "day 30 near-duplicate clusters",
            audit_a.duplicate_cluster_count,
            audit_b.duplicate_cluster_count,
        )
    )
    print(
        "%-34s %14d %14d"
        % (
            "day 30 ttl-overdue records",
            len(audit_a.ttl_overdue_ids),
            len(audit_b.ttl_overdue_ids),
        )
    )
    print(
        "%-34s %14d %14d"
        % ("day 30 orphan records", len(audit_a.orphan_ids), len(audit_b.orphan_ids))
    )
    print("")
    print("maintenance arm applied %d logged changes across %d passes."
          % (changes_b, sum(1 for r in arm_b if r.maintained)))
    print(
        "day-30 accuracy delta   : %+.1f percentage points in favour of maintenance"
        % (100.0 * (arm_b[-1].accuracy - arm_a[-1].accuracy))
    )
    print(
        "relative error reduction: %.1f%% of the day-30 error removed"
        % (
            100.0
            * (arm_b[-1].accuracy - arm_a[-1].accuracy)
            / (1.0 - arm_a[-1].accuracy)
            if arm_a[-1].accuracy < 1.0
            else 0.0
        )
    )
    print("")
    print("-" * 78)
    print("HEALTH SCORE BREAKDOWN, DAY 30, NO MAINTENANCE")
    print("-" * 78)
    print(audit_a.render())
    print("")
    print("-" * 78)
    print("HEALTH SCORE BREAKDOWN, DAY 30, MAINTENANCE EVERY %d DAYS" % MAINTENANCE_EVERY_DAYS)
    print("-" * 78)
    print(audit_b.render())
    print("")
    print("stores written to %s/ for inspection." % OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
