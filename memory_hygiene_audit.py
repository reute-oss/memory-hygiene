"""Read-only audit of an agent memory store.

The audit never mutates anything. It classifies the current, believed-true
records into the failure modes that make memory stores drift, and rolls them up
into a single Memory Health Score. The score's formula and every component's
contribution are printed, so a reader can disagree with the weights and change
them rather than trust a black box.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # package layout: memory_hygiene/audit.py
    from .store import MemoryRecord, MemoryStore, open_store, utc_now, _as_utc
except ImportError:  # flat layout: memory_hygiene_audit.py next to memory_hygiene_store.py
    from memory_hygiene_store import (
        MemoryRecord,
        MemoryStore,
        open_store,
        utc_now,
        _as_utc,
    )

__all__ = [
    "DEFAULT_TTL_POLICY",
    "AuditConfig",
    "AuditReport",
    "audit_store",
    "audit_records",
    "normalize_value",
    "similarity",
    "find_contradictions",
    "find_duplicate_clusters",
    "find_orphans",
    "find_ttl_overdue",
    "find_unbounded",
]

#: Default time-to-live per fact class. ``None`` means "no TTL, keep forever".
DEFAULT_TTL_POLICY: Dict[str, Optional[timedelta]] = {
    "ephemeral": timedelta(days=7),
    "status": timedelta(days=90),
    "preference": None,
    "identity": None,
}

#: Weights of the five health components. They sum to 100 by design.
HEALTH_WEIGHTS: Dict[str, float] = {
    "contradictions": 35.0,
    "duplicates": 20.0,
    "ttl_overdue": 25.0,
    "orphans": 12.0,
    "unbounded": 8.0,
}

HEALTH_FORMULA = (
    "score = 100 - ("
    "35*contradiction_rate + 20*duplicate_rate + 25*ttl_overdue_rate + "
    "12*orphan_rate + 8*unbounded_rate"
    "); every rate = affected_live_records / live_records"
)

_PUNCTUATION = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_value(value: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace.

    Used for both duplicate detection and equality checks so the two agree.
    """
    lowered = value.strip().lower()
    lowered = _PUNCTUATION.sub(" ", lowered)
    return _WHITESPACE.sub(" ", lowered).strip()


def similarity(left: str, right: str) -> float:
    """Return ``difflib`` similarity of two normalized values, in [0, 1]."""
    return SequenceMatcher(None, normalize_value(left), normalize_value(right)).ratio()


@dataclass
class AuditConfig:
    """Tunables for the audit.

    Attributes:
        now: Evaluation time. Everything (age, TTL, liveness) is relative to it.
        duplicate_threshold: ``difflib`` ratio at or above which two values in
            the same ``(entity, relation)`` group are treated as near-duplicates
            rather than as a contradiction.
        orphan_grace_days: A record is only an orphan once it is at least this
            old and has still never been retrieved.
        ttl_policy: Per-class TTL. Classes mapped to ``None``, or absent from
            the dict, have no TTL.
    """

    now: datetime
    duplicate_threshold: float = 0.90
    orphan_grace_days: float = 14.0
    ttl_policy: Dict[str, Optional[timedelta]] = field(
        default_factory=lambda: dict(DEFAULT_TTL_POLICY)
    )

    def ttl_for(self, fact_class: str) -> Optional[timedelta]:
        """TTL for a fact class, or ``None`` when the class is unbounded."""
        return self.ttl_policy.get(fact_class)


def group_by_key(records: Sequence[MemoryRecord]) -> Dict[str, List[MemoryRecord]]:
    """Group records by ``entity|relation``, each group sorted by write time."""
    groups: Dict[str, List[MemoryRecord]] = {}
    for record in records:
        groups.setdefault(record.key, []).append(record)
    for group in groups.values():
        group.sort(key=lambda r: (r.written_at, r.id))
    return groups


def find_duplicate_clusters(
    live: Sequence[MemoryRecord], threshold: float
) -> List[List[MemoryRecord]]:
    """Cluster near-duplicate records inside each ``(entity, relation)`` group.

    Clustering is single-linkage: a record joins a cluster if it is similar
    enough to any member. Only clusters of size >= 2 are returned.
    """
    clusters: List[List[MemoryRecord]] = []
    for _, group in sorted(group_by_key(live).items()):
        buckets: List[List[MemoryRecord]] = []
        for record in group:
            placed = False
            for bucket in buckets:
                if any(similarity(record.value, other.value) >= threshold for other in bucket):
                    bucket.append(record)
                    placed = True
                    break
            if not placed:
                buckets.append([record])
        clusters.extend(bucket for bucket in buckets if len(bucket) >= 2)
    return clusters


def find_contradictions(
    live: Sequence[MemoryRecord], threshold: float
) -> List[Tuple[MemoryRecord, MemoryRecord]]:
    """Find pairs asserting genuinely different values for the same key.

    A pair contradicts when both records are believed true, share
    ``(entity, relation)``, and their values are dissimilar enough that they
    cannot be a rewording of each other (similarity below ``threshold``).
    Near-duplicates are deliberately excluded here and counted as duplicates
    instead, so the two metrics never double-count the same pair.
    """
    pairs: List[Tuple[MemoryRecord, MemoryRecord]] = []
    for _, group in sorted(group_by_key(live).items()):
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                left, right = group[i], group[j]
                if normalize_value(left.value) == normalize_value(right.value):
                    continue
                if similarity(left.value, right.value) >= threshold:
                    continue
                pairs.append((left, right))
    return pairs


def find_orphans(
    live: Sequence[MemoryRecord], now: datetime, grace_days: float
) -> List[MemoryRecord]:
    """Records never retrieved since being written, older than the grace period."""
    return [
        r
        for r in live
        if r.retrieval_count == 0
        and r.last_retrieved_at is None
        and r.age_days(now) >= grace_days
    ]


def find_ttl_overdue(live: Sequence[MemoryRecord], config: AuditConfig) -> List[MemoryRecord]:
    """Live records whose class TTL already elapsed but which are still open."""
    overdue = []
    for record in live:
        ttl = config.ttl_for(record.fact_class)
        if ttl is None:
            continue
        if _as_utc(record.written_at) + ttl <= _as_utc(config.now):
            overdue.append(record)
    return overdue


def find_unbounded(live: Sequence[MemoryRecord], config: AuditConfig) -> List[MemoryRecord]:
    """Open-ended records whose class has no TTL policy at all.

    These are not wrong, but they can never expire by themselves, so they are
    the population from which stale facts are eventually drawn.
    """
    return [
        r for r in live if r.valid_to is None and config.ttl_for(r.fact_class) is None
    ]


def _age_distribution(live: Sequence[MemoryRecord], now: datetime) -> Dict[str, int]:
    """Bucket live records by age in days."""
    buckets = {"0-7d": 0, "8-30d": 0, "31-90d": 0, "90d+": 0}
    for record in live:
        age = record.age_days(now)
        if age <= 7:
            buckets["0-7d"] += 1
        elif age <= 30:
            buckets["8-30d"] += 1
        elif age <= 90:
            buckets["31-90d"] += 1
        else:
            buckets["90d+"] += 1
    return buckets


def _staleness_by_class(
    live: Sequence[MemoryRecord], config: AuditConfig
) -> Dict[str, Dict[str, Any]]:
    """Per-class counts, mean age, and share of TTL-overdue records."""
    by_class: Dict[str, List[MemoryRecord]] = {}
    for record in live:
        by_class.setdefault(record.fact_class, []).append(record)

    overdue_ids = {r.id for r in find_ttl_overdue(live, config)}
    out: Dict[str, Dict[str, Any]] = {}
    for fact_class in sorted(by_class):
        group = by_class[fact_class]
        ttl = config.ttl_for(fact_class)
        ages = [r.age_days(config.now) for r in group]
        overdue = sum(1 for r in group if r.id in overdue_ids)
        out[fact_class] = {
            "live_records": len(group),
            "ttl_days": None if ttl is None else round(ttl.total_seconds() / 86400.0, 2),
            "mean_age_days": round(sum(ages) / len(ages), 2) if ages else 0.0,
            "max_age_days": round(max(ages), 2) if ages else 0.0,
            "ttl_overdue": overdue,
            "ttl_overdue_pct": round(100.0 * overdue / len(group), 1) if group else 0.0,
        }
    return out


@dataclass
class AuditReport:
    """The full result of an audit run."""

    now: datetime
    total_records: int
    live_records: int
    closed_records: int
    contradiction_pairs: List[Tuple[str, str]]
    contradiction_keys: List[str]
    contradicting_record_ids: List[str]
    duplicate_clusters: List[List[str]]
    redundant_duplicate_ids: List[str]
    orphan_ids: List[str]
    ttl_overdue_ids: List[str]
    unbounded_ids: List[str]
    age_distribution: Dict[str, int]
    staleness_by_class: Dict[str, Dict[str, Any]]
    components: Dict[str, Dict[str, float]]
    health_score: float
    config: AuditConfig

    # -- derived convenience properties -------------------------------------

    @property
    def contradiction_count(self) -> int:
        """Number of contradicting pairs."""
        return len(self.contradiction_pairs)

    @property
    def duplicate_cluster_count(self) -> int:
        """Number of near-duplicate clusters."""
        return len(self.duplicate_clusters)

    def rate(self, component: str) -> float:
        """Affected-record rate of one health component."""
        return self.components[component]["rate"]

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dict, suitable for CI consumption."""
        return {
            "now": self.now.isoformat(),
            "health_score": self.health_score,
            "health_formula": HEALTH_FORMULA,
            "totals": {
                "total_records": self.total_records,
                "live_records": self.live_records,
                "closed_records": self.closed_records,
            },
            "contradictions": {
                "pairs": [list(pair) for pair in self.contradiction_pairs],
                "pair_count": self.contradiction_count,
                "affected_keys": self.contradiction_keys,
                "affected_record_ids": self.contradicting_record_ids,
            },
            "duplicates": {
                "clusters": self.duplicate_clusters,
                "cluster_count": self.duplicate_cluster_count,
                "redundant_record_ids": self.redundant_duplicate_ids,
            },
            "orphans": {"record_ids": self.orphan_ids, "count": len(self.orphan_ids)},
            "ttl_overdue": {
                "record_ids": self.ttl_overdue_ids,
                "count": len(self.ttl_overdue_ids),
            },
            "unbounded": {
                "record_ids": self.unbounded_ids,
                "count": len(self.unbounded_ids),
            },
            "age_distribution": self.age_distribution,
            "staleness_by_class": self.staleness_by_class,
            "components": self.components,
            "config": {
                "duplicate_threshold": self.config.duplicate_threshold,
                "orphan_grace_days": self.config.orphan_grace_days,
                "ttl_policy_days": {
                    name: (None if ttl is None else ttl.total_seconds() / 86400.0)
                    for name, ttl in sorted(self.config.ttl_policy.items())
                },
            },
        }

    def render(self) -> str:
        """Human-readable report."""
        lines: List[str] = []
        add = lines.append
        add("=" * 72)
        add("MEMORY HYGIENE AUDIT")
        add("=" * 72)
        add("evaluated at        : %s" % self.now.isoformat())
        add(
            "records             : %d total / %d believed true / %d closed"
            % (self.total_records, self.live_records, self.closed_records)
        )
        add(
            "duplicate threshold : %.2f   orphan grace: %.0f days"
            % (self.config.duplicate_threshold, self.config.orphan_grace_days)
        )
        add("")
        add("-- findings (over the %d believed-true records) --" % self.live_records)
        add(
            "contradiction pairs : %d  across %d entity/relation keys  (%d records, %.1f%%)"
            % (
                self.contradiction_count,
                len(self.contradiction_keys),
                len(self.contradicting_record_ids),
                100.0 * self.rate("contradictions"),
            )
        )
        add(
            "near-dup clusters   : %d  (%d redundant records, %.1f%%)"
            % (
                self.duplicate_cluster_count,
                len(self.redundant_duplicate_ids),
                100.0 * self.rate("duplicates"),
            )
        )
        add(
            "ttl overdue         : %d records (%.1f%%)"
            % (len(self.ttl_overdue_ids), 100.0 * self.rate("ttl_overdue"))
        )
        add(
            "orphans (never read): %d records (%.1f%%)"
            % (len(self.orphan_ids), 100.0 * self.rate("orphans"))
        )
        add(
            "unbounded, no TTL   : %d records (%.1f%%)"
            % (len(self.unbounded_ids), 100.0 * self.rate("unbounded"))
        )
        add("")
        add("-- age distribution (believed-true records) --")
        for bucket in ("0-7d", "8-30d", "31-90d", "90d+"):
            count = self.age_distribution.get(bucket, 0)
            pct = 100.0 * count / self.live_records if self.live_records else 0.0
            add("  %-7s %5d  %5.1f%%" % (bucket, count, pct))
        add("")
        add("-- staleness by fact class --")
        add(
            "  %-12s %6s %9s %10s %10s %9s"
            % ("class", "live", "ttl_days", "mean_age", "max_age", "overdue")
        )
        for fact_class, stats in self.staleness_by_class.items():
            add(
                "  %-12s %6d %9s %10.2f %10.2f %8.1f%%"
                % (
                    fact_class,
                    stats["live_records"],
                    "-" if stats["ttl_days"] is None else "%g" % stats["ttl_days"],
                    stats["mean_age_days"],
                    stats["max_age_days"],
                    stats["ttl_overdue_pct"],
                )
            )
        add("")
        add("-- memory health score --")
        add("  formula: %s" % HEALTH_FORMULA)
        add("  %-16s %8s %8s %10s" % ("component", "weight", "rate", "penalty"))
        total_penalty = 0.0
        for name in ("contradictions", "duplicates", "ttl_overdue", "orphans", "unbounded"):
            comp = self.components[name]
            total_penalty += comp["penalty"]
            add(
                "  %-16s %8.1f %8.4f %10.2f"
                % (name, comp["weight"], comp["rate"], comp["penalty"])
            )
        add("  %-16s %8s %8s %10.2f" % ("total penalty", "", "", total_penalty))
        add("")
        add("  MEMORY HEALTH SCORE: %.1f / 100" % self.health_score)
        add("=" * 72)
        return "\n".join(lines)


def audit_records(
    records: Sequence[MemoryRecord], config: AuditConfig
) -> AuditReport:
    """Audit an in-memory list of records.

    Args:
        records: Every record in the store, live or closed.
        config: Evaluation time and thresholds.

    Returns:
        A populated :class:`AuditReport`.
    """
    live = sorted(
        (r for r in records if r.is_believed_true(config.now)),
        key=lambda r: (r.entity, r.relation, r.written_at, r.id),
    )
    live_count = len(live)

    contradictions = find_contradictions(live, config.duplicate_threshold)
    contradicting_ids = sorted({r.id for pair in contradictions for r in pair})
    contradiction_keys = sorted({pair[0].key for pair in contradictions})

    clusters = find_duplicate_clusters(live, config.duplicate_threshold)
    redundant_ids = sorted(
        r.id
        for cluster in clusters
        for r in sorted(cluster, key=lambda x: (_as_utc(x.written_at), x.id))[1:]
    )

    orphans = find_orphans(live, config.now, config.orphan_grace_days)
    overdue = find_ttl_overdue(live, config)
    unbounded = find_unbounded(live, config)

    def rate(count: int) -> float:
        return (count / live_count) if live_count else 0.0

    raw_rates = {
        "contradictions": rate(len(contradicting_ids)),
        "duplicates": rate(len(redundant_ids)),
        "ttl_overdue": rate(len(overdue)),
        "orphans": rate(len(orphans)),
        "unbounded": rate(len(unbounded)),
    }

    components: Dict[str, Dict[str, float]] = {}
    penalty_total = 0.0
    for name, weight in HEALTH_WEIGHTS.items():
        component_rate = raw_rates[name]
        penalty = weight * component_rate
        penalty_total += penalty
        components[name] = {
            "weight": weight,
            "rate": round(component_rate, 6),
            "penalty": round(penalty, 4),
        }

    score = max(0.0, min(100.0, 100.0 - penalty_total))

    return AuditReport(
        now=config.now,
        total_records=len(records),
        live_records=live_count,
        closed_records=len(records) - live_count,
        contradiction_pairs=[(a.id, b.id) for a, b in contradictions],
        contradiction_keys=contradiction_keys,
        contradicting_record_ids=contradicting_ids,
        duplicate_clusters=[
            [r.id for r in sorted(cluster, key=lambda x: (x.written_at, x.id))]
            for cluster in clusters
        ],
        redundant_duplicate_ids=redundant_ids,
        orphan_ids=sorted(r.id for r in orphans),
        ttl_overdue_ids=sorted(r.id for r in overdue),
        unbounded_ids=sorted(r.id for r in unbounded),
        age_distribution=_age_distribution(live, config.now),
        staleness_by_class=_staleness_by_class(live, config),
        components=components,
        health_score=round(score, 1),
        config=config,
    )


def audit_store(store: MemoryStore, config: AuditConfig) -> AuditReport:
    """Audit a store. Read-only: nothing is written back."""
    return audit_records(store.all_records(), config)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory-hygiene-audit",
        description="Read-only health audit of an agent memory store.",
    )
    parser.add_argument("--store", required=True, help="path to the store file")
    parser.add_argument(
        "--backend", default="json", choices=("json", "sqlite"), help="store backend"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.90,
        help="near-duplicate similarity threshold (default: 0.90)",
    )
    parser.add_argument(
        "--orphan-grace-days",
        type=float,
        default=14.0,
        help="minimum age before an unread record counts as an orphan",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="ISO timestamp to evaluate at (default: current UTC time)",
    )
    parser.add_argument(
        "--fail-below",
        type=float,
        default=None,
        help="exit 1 if the health score is below this value",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns the process exit code."""
    args = _build_parser().parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else utc_now()
    config = AuditConfig(
        now=now,
        duplicate_threshold=args.threshold,
        orphan_grace_days=args.orphan_grace_days,
    )
    store = open_store(args.backend, args.store)
    report = audit_store(store, config)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(report.render())

    if args.fail_below is not None and report.health_score < args.fail_below:
        print(
            "FAIL: health score %.1f is below threshold %.1f"
            % (report.health_score, args.fail_below),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
