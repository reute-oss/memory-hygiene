# memory-hygiene

Measure and repair drift in an agent memory store. Pure Python standard
library, no dependencies, drops into any codebase.

Companion code for the article on Medium:
[Your agent's memory is drifting, and your dashboards look fine](https://medium.com/@reute/your-agents-memory-is-drifting-and-your-dashboards-look-fine-4ee1812868c7).

## Disclaimer

Disclaimer: This code is provided "as-is" as a demonstration only to illustrate a potential solution. The code does not constitute a Google product or service of any kind, and Google offers no support, warranties, or liability of any kind with its regard. Whoever chooses to use this code accepts all responsibility related to it, including for its implementation, use, and ongoing maintenance. For the avoidance of doubt, this code is not eligible for the Google Open Source Software Vulnerability Rewards Program.

Agent memory stores do not usually fail at retrieval. They fail because a fact
gets written, is later superseded, and nothing ever invalidates the old one.
Both records then sit in the store looking equally retrievable. Add
near-duplicate memories from repeated sessions and facts that were written once
and never read again, and accuracy quietly halves over a month while every
dashboard still looks fine.

This package gives you two things:

1. **`audit`** tells you how far your store has already drifted, and shows its work.
2. **`maintain`** is an idempotent pass you run on a schedule to keep it healthy.

## What the simulation measured

Numbers from `demo_drift.py`, seed `20260930`, 30 simulated days, two arms over
an identical event stream. Full output is in `RESULTS.md`.

| | no maintenance | maintenance every 7 days |
|---|---|---|
| day 1 retrieval accuracy | 100.0% | 100.0% |
| day 30 retrieval accuracy | **70.9%** | **97.1%** |
| day 30 memory health score | 66.4 | 90.9 |
| day 30 contradiction pairs | 185 | 7 |
| day 30 records believed true | 310 | 150 |

That is 26.2 percentage points, which is 90.0% of the day-30 error removed.

## 60-second quickstart

```bash
# 1. see how bad it is (read-only, never writes)
python3 memory_hygiene_audit.py --store mystore.json

# 2. see what a maintenance pass would change (dry run is the default)
python3 memory_hygiene_maintain.py --store mystore.json

# 3. actually do it, with a reversible log of every change
python3 memory_hygiene_maintain.py --store mystore.json --apply --log maint.jsonl

# 4. use it as a CI gate
python3 memory_hygiene_audit.py --store mystore.json --json --fail-below 80
```

`--apply` is the only way to mutate anything. Without it nothing is written,
not even the log.

From Python:

```python
from datetime import datetime
from memory_hygiene_audit import AuditConfig, audit_store
from memory_hygiene_maintain import MaintenancePolicy, run_maintenance
from memory_hygiene_store import JsonMemoryStore

store = JsonMemoryStore("mystore.json")
now = datetime.utcnow()

report = audit_store(store, AuditConfig(now=now))
print(report.render())
print(report.health_score)

result = run_maintenance(store, MaintenancePolicy(now=now), dry_run=False,
                         log_path="maint.jsonl")
print(result.render())
```

## Plugging in your own store

Implement three methods and everything else works unchanged:

```python
from memory_hygiene_store import MemoryStore, MemoryRecord

class MyStore(MemoryStore):
    def all_records(self) -> list[MemoryRecord]: ...
    def update(self, record: MemoryRecord) -> None: ...   # upsert by id
    def delete(self, record_id: str) -> None: ...         # missing id is a no-op
```

`JsonMemoryStore` and `SqliteMemoryStore` ship as working references. Map your
own rows onto `MemoryRecord`:

| field | meaning |
|---|---|
| `id` | stable unique id |
| `entity` / `relation` / `value` | the fact, as a triple |
| `written_at` | when the agent learned it |
| `valid_from` / `valid_to` | believed-true window. `valid_to = None` means still believed |
| `fact_class` | drives TTL: `identity`, `preference`, `status`, `ephemeral` |
| `last_retrieved_at` / `retrieval_count` | read telemetry, used for orphans and merges |
| `source_session` | who wrote it |

The whole approach rests on `valid_to`. If your store has no way to say "this
was true until then", you cannot invalidate anything and the drift is structural.

## What the health score means

One number, 0 to 100, printed together with its formula and every component's
contribution. It is a weighted penalty, not a probability:

```
score = 100 - ( 35*contradiction_rate
              + 20*duplicate_rate
              + 25*ttl_overdue_rate
              + 12*orphan_rate
              +  8*unbounded_rate )

every rate = affected believed-true records / believed-true records
```

| component | weight | what it counts |
|---|---|---|
| contradictions | 35 | records in a pair with the same `(entity, relation)`, genuinely different values, both still believed true |
| duplicates | 20 | redundant members of a near-duplicate cluster (cluster size minus one) |
| ttl_overdue | 25 | still-open records whose class TTL already elapsed |
| orphans | 12 | never retrieved since being written, and older than the grace period |
| unbounded | 8 | open-ended records whose class has no TTL at all, so they can never expire on their own |

Weights sum to 100, so the score is 100 when nothing is wrong and 0 when every
live record trips every check. Contradictions carry the most weight because
they are the only failure mode that makes retrieval return a confidently wrong
answer. `unbounded` carries the least because it is a risk, not a defect.

Disagree with the weights. They are one dict in `audit.py` (`HEALTH_WEIGHTS`),
and the report prints each contribution separately so you can recompute by
hand.

**Contradictions and duplicates never double-count.** Two records with the same
`(entity, relation)` but different values are a contradiction only if their
values are *dissimilar*. If they are similar enough to be a rewording, they are
counted as a near-duplicate instead. One threshold, default 0.90, decides.

## What the maintenance pass does

Three steps, in this order:

1. **Merge near-duplicates.** The survivor keeps the earliest member's `id`,
   so existing references still resolve, and takes the cluster's *latest*
   `written_at` and `valid_from`, the *sum* of `retrieval_count`, the max
   `last_retrieved_at` and the longest value. `valid_to` takes the latest end
   in the cluster, where a still-open record outranks every closed one, so
   absorbing an older closed copy can never shorten a live belief. Absorbed
   ids are recorded in `merged_from` and in the log.
2. **Close superseded intervals.** Within one `(entity, relation)`, sort the
   still-open records by write time and close each one at the moment the next
   was written. Exactly one open belief survives. Chains of three or more work.
3. **Apply per-class TTL.** Close open records whose class TTL already elapsed.
   Records not yet due are left alone. Configure with `--ttl '{"ephemeral": 7,
   "status": 90, "preference": null}'`, where `null` means never expires.

**Idempotent by construction.** Each step only touches records that are still
open and still in conflict, and each step closes or removes what triggered it.
The second run finds nothing to do. Verified by test and end to end: a second
`--apply` on the same store reported 0 changes and appended 0 log lines.

**Reversible.** Every change is a JSONL line with `before`, `after`, the action
and a plain reason, so the log is both an audit trail and enough information to
roll back.

## Running the tests and the simulation

```bash
python3 test_hygiene.py                      # 48 tests
python3 test_memorybank_adapter.py           # 53 tests, offline, no SDK needed
python3 demo_drift.py                        # deterministic, seed 20260930
```

The simulation prints byte-identical stdout on every run. There is no wall
clock and no filesystem state in the output.

## Python versions and the Memory Bank extra

The core package is standard library only and runs on Python 3.9 and up.

The Agent Platform Memory Bank adapter, `MemoryBankStore` in
`memory_hygiene_memorybank.py`, needs the Google SDK. Nothing is published to
PyPI, so install the extra from your clone of the repo:

```
pip install -e ".[memorybank]"
```

That extra needs Python 3.10 or newer, because the SDK does. On 3.9 pip skips
the SDK without an error, and the adapter reports the missing SDK when you
construct it. The adapter was verified against a live Memory Bank on
7 October 2026, see `RESULTS.md`.

## Limitations

Be honest about what this is. It is a heuristic hygiene tool, not a correctness
proof.

- **`difflib` similarity is not semantic similarity.** `SequenceMatcher` compares
  characters. "flight is confirmed" and "flight is cancelled" score high and
  could be merged as duplicates; "prefers Berlin" and "lives in Germany" score
  low and are treated as unrelated. Tune the threshold against your own data and
  look at what it merges before you trust `--apply`.
- **Contradiction detection assumes a sane `(entity, relation)` schema.** If your
  relations are free text, or one relation legitimately holds several values at
  once (tags, group memberships, permissions), this will report contradictions
  that are not contradictions and close facts that should stay open. Exclude
  those relations, or model them as separate keys.
- **Newer is assumed to be righter.** Supersession closes the older record. A
  late-arriving correction about the past, or an out-of-order write, breaks that
  assumption.
- **The score is a heuristic with invented weights.** It is useful as a trend
  line and a CI gate. It is not calibrated against anything, and a score of 85
  does not mean 85% of answers are right.
- **The `unbounded` component behaves counterintuitively, on purpose.** In the
  simulation it was 53.2% before maintenance and 58.7% after. Maintenance closes
  the bounded classes, so the never-expiring preference and identity facts become
  a *larger* share of what is left. It is measuring risk concentration, not damage.
- **Duplicates do not hurt accuracy in the simulation's model**, and the numbers
  show it. A reworded copy of a true fact is still the true fact. Deduplication
  buys storage, prompt budget and a cleaner store. The accuracy win comes from
  closing contradictions and expiring stale facts.
- **The simulation is synthetic.** It is a reproducible model of a drift pattern,
  with a deliberately naive retriever that cannot tell a live fact from a dead
  one. Your store's real drift curve depends on your write patterns. Run the
  audit on your own data; that number is the real one.
- **Merging loses information.** The losing records are deleted, not archived.
  Their ids survive in `merged_from` and their full contents in the log, so keep
  the log.
