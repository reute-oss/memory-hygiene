"""The scheduled maintenance pass.

Three operations, in a fixed order:

1. **Merge near-duplicates** - collapse restatements of the same fact.
2. **Close superseded intervals** - when several different values are all
   believed true for one ``(entity, relation)``, close each older record at
   the moment the next one was written, leaving exactly one open belief.
3. **Apply per-class TTL** - close open records whose class TTL has elapsed.

The pass is idempotent: every operation only touches records that are still
open and still in conflict, and each operation closes or removes the records
that triggered it, so a second run finds nothing to do.

``--dry-run`` is the default. Nothing is written, not even the audit log,
unless ``--apply`` is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

try:  # package layout
    from .audit import (
        DEFAULT_TTL_POLICY,
        AuditConfig,
        AuditReport,
        audit_records,
        audit_store,
        find_duplicate_clusters,
        group_by_key,
        normalize_value,
    )
    from .store import MemoryRecord, MemoryStore, open_store, utc_now, _as_utc
except ImportError:  # flat layout
    from memory_hygiene_audit import (
        DEFAULT_TTL_POLICY,
        AuditConfig,
        AuditReport,
        audit_records,
        audit_store,
        find_duplicate_clusters,
        group_by_key,
        normalize_value,
    )
    from memory_hygiene_store import (
        MemoryRecord,
        MemoryStore,
        open_store,
        utc_now,
        _as_utc,
    )

__all__ = [
    "MaintenancePolicy",
    "Change",
    "MaintenanceResult",
    "run_maintenance",
]


@dataclass
class MaintenancePolicy:
    """Configuration for one maintenance pass.

    Attributes:
        now: The clock the pass runs against.
        ttl_policy: Per fact class TTL; ``None`` means the class never expires.
        duplicate_threshold: Similarity at or above which two values in the
            same ``(entity, relation)`` group are merged.
        orphan_grace_days: Passed through to the audit; orphans are reported
            but never deleted by this pass.
        close_superseded: Enable step 2.
        merge_duplicates: Enable step 1.
        apply_ttl: Enable step 3.
    """

    now: datetime
    ttl_policy: Dict[str, Optional[timedelta]] = field(
        default_factory=lambda: dict(DEFAULT_TTL_POLICY)
    )
    duplicate_threshold: float = 0.90
    orphan_grace_days: float = 14.0
    close_superseded: bool = True
    merge_duplicates: bool = True
    apply_ttl: bool = True

    def audit_config(self) -> AuditConfig:
        """The matching audit configuration, so scores and actions agree."""
        return AuditConfig(
            now=self.now,
            duplicate_threshold=self.duplicate_threshold,
            orphan_grace_days=self.orphan_grace_days,
            ttl_policy=dict(self.ttl_policy),
        )


@dataclass
class Change:
    """One reversible change proposed or applied by the pass.

    Attributes:
        action: ``"close_superseded"``, ``"merge_duplicate"``,
            ``"absorb_duplicate"`` or ``"ttl_close"``.
        record_id: The record the change applies to.
        entity: Entity of that record, for readability in the log.
        relation: Relation of that record.
        before: Field values before the change.
        after: Field values after the change.
        reason: Short human explanation.
    """

    action: str
    record_id: str
    entity: str
    relation: str
    before: Dict[str, Any]
    after: Dict[str, Any]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dict for the audit log."""
        return {
            "action": self.action,
            "record_id": self.record_id,
            "entity": self.entity,
            "relation": self.relation,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
        }


@dataclass
class MaintenanceResult:
    """Outcome of one pass."""

    dry_run: bool
    now: datetime
    changes: List[Change]
    deleted_ids: List[str]
    audit_before: AuditReport
    audit_after: AuditReport

    @property
    def change_count(self) -> int:
        """Number of individual record changes."""
        return len(self.changes)

    def counts_by_action(self) -> Dict[str, int]:
        """How many changes of each action type."""
        counts: Dict[str, int] = {}
        for change in self.changes:
            counts[change.action] = counts.get(change.action, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe summary."""
        return {
            "dry_run": self.dry_run,
            "now": self.now.isoformat(),
            "change_count": self.change_count,
            "counts_by_action": self.counts_by_action(),
            "deleted_ids": self.deleted_ids,
            "health_score_before": self.audit_before.health_score,
            "health_score_after": self.audit_after.health_score,
            "live_records_before": self.audit_before.live_records,
            "live_records_after": self.audit_after.live_records,
            "changes": [c.to_dict() for c in self.changes],
        }

    def render(self) -> str:
        """Human-readable summary."""
        lines: List[str] = []
        add = lines.append
        add("=" * 72)
        add("MEMORY MAINTENANCE PASS  (%s)" % ("DRY RUN" if self.dry_run else "APPLIED"))
        add("=" * 72)
        add("ran at              : %s" % self.now.isoformat())
        add("changes             : %d" % self.change_count)
        counts = self.counts_by_action()
        for action in sorted(counts):
            add("  %-20s %d" % (action, counts[action]))
        add("records deleted     : %d" % len(self.deleted_ids))
        add(
            "live records        : %d -> %d"
            % (self.audit_before.live_records, self.audit_after.live_records)
        )
        add(
            "contradiction pairs : %d -> %d"
            % (
                self.audit_before.contradiction_count,
                self.audit_after.contradiction_count,
            )
        )
        add(
            "near-dup clusters   : %d -> %d"
            % (
                self.audit_before.duplicate_cluster_count,
                self.audit_after.duplicate_cluster_count,
            )
        )
        add(
            "ttl overdue         : %d -> %d"
            % (
                len(self.audit_before.ttl_overdue_ids),
                len(self.audit_after.ttl_overdue_ids),
            )
        )
        add(
            "health score        : %.1f -> %.1f"
            % (self.audit_before.health_score, self.audit_after.health_score)
        )
        add("=" * 72)
        return "\n".join(lines)


def _merge_duplicates(
    records: List[MemoryRecord], policy: MaintenancePolicy
) -> (List[Change], List[str]):
    """Collapse near-duplicate clusters in place.

    A cluster is a set of restatements of one and the same fact. Collapsing
    them must not lose the fact's *recency*: the cluster as a whole was last
    asserted when its newest member was written, and the later supersession
    and TTL steps both read ``written_at`` to decide who wins and what has
    expired. Folding a cluster into its earliest copy therefore made a freshly
    restated fact look stale, and a value it had already replaced won instead.

    So the survivor is a merge of the whole cluster, not just one member:

    * ``id`` stays the earliest member's id, so references into the audit log
      and ``merged_from`` chains elsewhere keep resolving.
    * ``written_at`` and ``valid_from`` take the **latest** in the cluster.
      That is when this fact was last asserted and last known to hold.
    * ``valid_to`` takes the **latest** end in the cluster, where an open
      record (``None``) counts as the latest end of all. Merging restatements
      is not new evidence that a belief ended, so absorbing a copy that was
      closed earlier must never shorten a copy that is still open.
    * ``retrieval_count`` is the **sum**: each copy was read on its own, and
      the merged record stands for all of those reads.
    * ``last_retrieved_at`` is the max, and the value is the longest spelling.

    The absorbed ids are recorded on the winner's ``merged_from`` and their
    records are dropped.
    """
    changes: List[Change] = []
    deleted: List[str] = []
    by_id = {r.id: r for r in records}
    live = [r for r in records if r.is_believed_true(policy.now)]

    for cluster in find_duplicate_clusters(live, policy.duplicate_threshold):
        ordered = sorted(cluster, key=lambda r: (_as_utc(r.written_at), r.id))
        winner = ordered[0]
        losers = ordered[1:]
        before = {
            "value": winner.value,
            "retrieval_count": winner.retrieval_count,
            "last_retrieved_at": winner.last_retrieved_at.isoformat()
            if winner.last_retrieved_at
            else None,
            "merged_from": list(winner.merged_from),
            "written_at": winner.written_at.isoformat(),
            "valid_from": winner.valid_from.isoformat() if winner.valid_from else None,
            "valid_to": winner.valid_to.isoformat() if winner.valid_to else None,
        }
        longest = max(ordered, key=lambda r: (len(r.value), r.id)).value
        retrieval_count = sum(r.retrieval_count for r in ordered)
        seen_at = [r.last_retrieved_at for r in ordered if r.last_retrieved_at is not None]
        # Recency of the cluster: the newest assertion of this same fact.
        latest_written = max(r.written_at for r in ordered if r.written_at is not None)
        valid_froms = [r.valid_from for r in ordered if r.valid_from is not None]
        latest_valid_from = max(valid_froms, key=_as_utc) if valid_froms else None
        # An open record (valid_to is None) outranks every closed one.
        if any(r.valid_to is None for r in ordered):
            latest_valid_to = None
        else:
            latest_valid_to = max((r.valid_to for r in ordered), key=_as_utc)
        winner.value = longest
        winner.retrieval_count = retrieval_count
        winner.last_retrieved_at = max(seen_at, key=_as_utc) if seen_at else None
        winner.written_at = latest_written
        winner.valid_from = latest_valid_from
        winner.valid_to = latest_valid_to
        winner.merged_from = sorted(set(winner.merged_from) | {r.id for r in losers})
        after = {
            "value": winner.value,
            "retrieval_count": winner.retrieval_count,
            "last_retrieved_at": winner.last_retrieved_at.isoformat()
            if winner.last_retrieved_at
            else None,
            "merged_from": list(winner.merged_from),
            "written_at": winner.written_at.isoformat(),
            "valid_from": winner.valid_from.isoformat() if winner.valid_from else None,
            "valid_to": winner.valid_to.isoformat() if winner.valid_to else None,
        }
        changes.append(
            Change(
                action="merge_duplicate",
                record_id=winner.id,
                entity=winner.entity,
                relation=winner.relation,
                before=before,
                after=after,
                reason="absorbed %d near-duplicate(s) at similarity >= %.2f"
                % (len(losers), policy.duplicate_threshold),
            )
        )
        for loser in losers:
            changes.append(
                Change(
                    action="absorb_duplicate",
                    record_id=loser.id,
                    entity=loser.entity,
                    relation=loser.relation,
                    before=loser.to_dict(),
                    after={"deleted": True, "merged_into": winner.id},
                    reason="near-duplicate of %s" % winner.id,
                )
            )
            deleted.append(loser.id)
            by_id.pop(loser.id, None)

    if deleted:
        dropped = set(deleted)
        records[:] = [r for r in records if r.id not in dropped]
    return changes, deleted


def _close_superseded(
    records: List[MemoryRecord], policy: MaintenancePolicy
) -> List[Change]:
    """Close older beliefs that a newer, different belief has replaced."""
    changes: List[Change] = []
    live = [r for r in records if r.is_believed_true(policy.now)]

    for _, group in sorted(group_by_key(live).items()):
        if len(group) < 2:
            continue
        distinct_values = {normalize_value(r.value) for r in group}
        if len(distinct_values) < 2:
            continue
        # group is sorted by (written_at, id); everything but the newest is
        # superseded by whatever came next.
        for older, newer in zip(group, group[1:]):
            if normalize_value(older.value) == normalize_value(newer.value):
                continue
            if newer.written_at < older.written_at:
                continue
            before = {"valid_to": None}
            older.valid_to = newer.written_at
            changes.append(
                Change(
                    action="close_superseded",
                    record_id=older.id,
                    entity=older.entity,
                    relation=older.relation,
                    before=before,
                    after={"valid_to": older.valid_to.isoformat()},
                    reason="superseded by %s written at %s"
                    % (newer.id, newer.written_at.isoformat()),
                )
            )
    return changes


def _apply_ttl(records: List[MemoryRecord], policy: MaintenancePolicy) -> List[Change]:
    """Close open records whose per-class TTL has already elapsed."""
    changes: List[Change] = []
    for record in records:
        if record.valid_to is not None:
            continue
        ttl = policy.ttl_policy.get(record.fact_class)
        if ttl is None:
            continue
        expiry = record.written_at + ttl
        if _as_utc(expiry) > _as_utc(policy.now):
            continue
        record.valid_to = expiry
        changes.append(
            Change(
                action="ttl_close",
                record_id=record.id,
                entity=record.entity,
                relation=record.relation,
                before={"valid_to": None},
                after={"valid_to": expiry.isoformat()},
                reason="class %r TTL of %g day(s) elapsed"
                % (record.fact_class, ttl.total_seconds() / 86400.0),
            )
        )
    return changes


def _append_log(log_path: str, now: datetime, changes: Sequence[Change]) -> None:
    """Append one JSON line per change to the maintenance audit log."""
    parent = os.path.dirname(os.path.abspath(log_path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    stamp = now.isoformat()
    with open(log_path, "a", encoding="utf-8") as handle:
        for change in changes:
            entry = {"ran_at": stamp}
            entry.update(change.to_dict())
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


def run_maintenance(
    store: MemoryStore,
    policy: MaintenancePolicy,
    dry_run: bool = True,
    log_path: Optional[str] = None,
) -> MaintenanceResult:
    """Run one maintenance pass.

    Args:
        store: The store to maintain.
        policy: TTLs, thresholds and which steps to run.
        dry_run: When True (default) nothing is written back and no log is
            produced; the returned result still lists every planned change.
        log_path: JSONL audit log to append to. Ignored on a dry run.

    Returns:
        A :class:`MaintenanceResult` with the before and after audits.
    """
    original = store.all_records()
    audit_before = audit_records(original, policy.audit_config())

    working = [r.copy() for r in original]
    changes: List[Change] = []
    deleted: List[str] = []

    if policy.merge_duplicates:
        merge_changes, merge_deleted = _merge_duplicates(working, policy)
        changes.extend(merge_changes)
        deleted.extend(merge_deleted)
    if policy.close_superseded:
        changes.extend(_close_superseded(working, policy))
    if policy.apply_ttl:
        changes.extend(_apply_ttl(working, policy))

    audit_after = audit_records(working, policy.audit_config())

    if not dry_run:
        touched = {c.record_id for c in changes if c.action != "absorb_duplicate"}
        for record in working:
            if record.id in touched:
                store.update(record)
        for record_id in deleted:
            store.delete(record_id)
        if log_path and changes:
            _append_log(log_path, policy.now, changes)

    return MaintenanceResult(
        dry_run=dry_run,
        now=policy.now,
        changes=changes,
        deleted_ids=deleted,
        audit_before=audit_before,
        audit_after=audit_after,
    )


def _parse_ttl_policy(raw: Optional[str]) -> Dict[str, Optional[timedelta]]:
    """Parse ``--ttl`` JSON like ``{"ephemeral": 7, "preference": null}``."""
    if not raw:
        return dict(DEFAULT_TTL_POLICY)
    parsed = json.loads(raw)
    policy: Dict[str, Optional[timedelta]] = {}
    for fact_class, days in parsed.items():
        policy[str(fact_class)] = None if days is None else timedelta(days=float(days))
    return policy


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory-hygiene-maintain",
        description="Idempotent maintenance pass over an agent memory store.",
    )
    parser.add_argument("--store", required=True, help="path to the store file")
    parser.add_argument(
        "--backend", default="json", choices=("json", "sqlite"), help="store backend"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write changes back (default is a dry run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit dry run; this is already the default",
    )
    parser.add_argument("--log", default=None, help="JSONL maintenance audit log path")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.90,
        help="near-duplicate similarity threshold (default: 0.90)",
    )
    parser.add_argument(
        "--ttl",
        default=None,
        help='TTL policy as JSON days per class, e.g. \'{"ephemeral": 7, "status": 90}\'',
    )
    parser.add_argument(
        "--now", default=None, help="ISO timestamp to run against (default: UTC now)"
    )
    parser.add_argument(
        "--fail-below",
        type=float,
        default=None,
        help="exit 1 if the post-pass health score is below this value",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns the process exit code."""
    args = _build_parser().parse_args(argv)
    if args.apply and args.dry_run:
        print("error: --apply and --dry-run are mutually exclusive", file=sys.stderr)
        return 2

    now = datetime.fromisoformat(args.now) if args.now else utc_now()
    policy = MaintenancePolicy(
        now=now,
        ttl_policy=_parse_ttl_policy(args.ttl),
        duplicate_threshold=args.threshold,
    )
    store = open_store(args.backend, args.store)
    result = run_maintenance(
        store, policy, dry_run=not args.apply, log_path=args.log
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(result.render())

    if args.fail_below is not None and result.audit_after.health_score < args.fail_below:
        print(
            "FAIL: health score %.1f is below threshold %.1f"
            % (result.audit_after.health_score, args.fail_below),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
