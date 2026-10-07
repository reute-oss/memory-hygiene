# RESULTS

Real captured output. Every number below was produced by running the code in
this repository. Nothing here is estimated or transcribed by hand: this file is
assembled directly from the captured stdout of the commands shown. The only
exceptions are the two superseded hashes in the Determinism section, which are
labelled there as unverified history and are not claims about the current
code.

## Environment

- Python 3.9.6 (Clang 17.0.0), macOS 15.7.9, arm64
- Standard library only. No third-party packages are installed or required.

## Exact commands

```bash
# the simulation, run twice to prove determinism
python3 -B demo_drift.py > final_demo.txt
python3 -B demo_drift.py > final_demo2.txt
cmp final_demo.txt final_demo2.txt

# the recorded hash, computed with the standard library
python3 -B -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" final_demo.txt

# the tests
python3 -B test_hygiene.py
```

## Determinism

**Seed: `20260930`** (declared in `demo_drift.py` as `SEED`).

Two consecutive runs produced byte-identical stdout. `cmp` reported no
difference and both runs hash to:

```
c9ea071d4901785e82c1b582887e94eee3956b76855a8bdc9c3fdfb94ce46638
```

That is SHA-256 over the 8019 bytes the simulation writes to stdout, computed
with `hashlib`. The captured stdout stored in this repository as `run_a.txt`
hashes to the same value, so the verbatim block at the bottom of this file and
the figures above it come from the same bytes.

These hashes were captured from the flat-file layout (`memory_hygiene_store.py`,
`memory_hygiene_audit.py`, `memory_hygiene_maintain.py` and `demo_drift.py` in
one directory), which is the layout published here.

Two earlier captures of this file recorded the hashes
`98e1b2b2c0ebe9dc2ff66e94a8a1de99986bed9904b8ed2d823117a9a5d1eda9` and
`8c27d24177d029d6ba55feb87bd88bb2128ce58719b1e931ce16661894412b08`. Both
predate the duplicate-merge recency fix, which changed what the simulation
prints, so neither describes the current code. They are kept here only as a
record of what was published before. Neither has been re-verified: the pre-fix
code is not in this repository any more, so no claim is made about either
value beyond the fact that it was once written down.

There is no wall clock and no filesystem state in the output, which is why this
holds.

## Headline numbers

| | no maintenance | maintenance every 7 days |
|---|---|---|
| day 1 retrieval accuracy | 100.0% | 100.0% |
| day 30 retrieval accuracy | **70.9%** | **97.1%** |
| accuracy change, day 1 to day 30 | -29.1pp | -2.9pp |
| day 30 memory health score | 66.4 | 90.9 |
| day 30 records stored | 310 | 251 |
| day 30 records believed true | 310 | 150 |
| day 30 contradiction pairs | 185 | 7 |
| day 30 near-duplicate clusters | 66 | 4 |
| day 30 ttl-overdue records | 42 | 1 |
| day 30 orphan records | 20 | 4 |

Day-30 delta: **+26.2 percentage points** in favour of maintenance, which is
**90.0% of the day-30 error removed**. The maintenance arm applied 213 logged
changes across 4 passes (days 7, 14, 21 and 28).

The maintenance arm is not flat at 100%. It sawtooths: each pass restores it to
near-perfect and it drifts down again until the next one. Day 7 hits 100.0%,
day 13 has drifted to 86.3%, day 14 restores 100.0%. That is the real shape of
running hygiene on a cadence rather than continuously.

### One-shot catch-up on an already drifted store

The day-30 no-maintenance store was also run through a single maintenance pass,
to answer "what if I never did this and start now?":

```bash
python3 -B memory_hygiene_maintain.py --store work.json --apply \
    --log maint.jsonl --now 2026-01-30T20:00:00
```

- health score **66.4 to 95.0**
- contradiction pairs **185 to 0**
- near-duplicate clusters **66 to 0**
- ttl-overdue records **42 to 0**
- records believed true **310 to 137**
- 239 changes logged, 82 records deleted as absorbed duplicates

Re-running the identical command immediately afterwards reported **0 changes**
and appended **0 new log lines**, confirming idempotency end to end and not
only in the unit tests.

### CLI behaviour verified

- `--dry-run` is the default. After a dry run the store file hashed identically
  to before it (`ca3604c3a261251f...` both times), and no log file was created.
- A dry run and an `--apply` run over the same input planned the same change
  count, the same action breakdown and the same resulting health score.
- CI gate exit codes: `--fail-below 90` exited 0 against a 95.0 store,
  `--fail-below 99` exited 1. Same behaviour for both `audit` and `maintain`.

## Test run

**48 tests, all passing** (0.031s). The suite comes out clean.

The suite covers the four drift mechanisms, both store backends, the CLI
surfaces, the 30 day simulation, and two regression classes added after review:

- `TestRestatementRecency`, 6 tests. Cover the duplicate-merge recency defect,
  where collapsing a cluster into its earliest copy made a restated fact look
  older than a value it had already superseded. The supersession step then
  closed the wrong record and left the agent believing a stale value.
- `TestTimezoneAwareClock`, 5 tests. Cover naive and aware datetimes meeting at
  the TTL comparison, in both directions, through the library and the CLI.

Both classes failed before their respective fixes and pass after, which is the
only reason to trust them.

### Verbatim tail

```
Ran 48 tests in 0.031s

OK
```

## Verbatim: `python3 -B test_hygiene.py`

Captured from stderr, which is where `unittest` writes. The four tracebacks are
omitted because they carry absolute paths from the machine that ran them; the
failing frame and the exception are quoted in full above. One test also prints
a maintenance report to stdout that contains a wall-clock timestamp, so that
part of a live terminal session is not reproducible and is not reproduced here.

```
test_closed_record_is_not_a_contradiction (__main__.TestContradictionDetection) ... ok
test_contradiction_drives_the_health_score_down (__main__.TestContradictionDetection) ... ok
test_detects_open_contradiction (__main__.TestContradictionDetection) ... ok
test_different_entities_do_not_contradict (__main__.TestContradictionDetection) ... ok
test_near_duplicates_are_not_counted_as_contradictions (__main__.TestContradictionDetection) ... ok
test_dry_run_and_apply_plan_the_same_changes (__main__.TestDryRun) ... ok
test_dry_run_is_the_default (__main__.TestDryRun) ... ok
test_dry_run_leaves_the_backing_file_untouched (__main__.TestDryRun) ... ok
test_dry_run_leaves_the_store_untouched (__main__.TestDryRun) ... ok
test_dry_run_writes_no_log (__main__.TestDryRun) ... ok
test_distinct_values_are_never_merged (__main__.TestDuplicateMerging) ... ok
test_merge_keeps_latest_written_at_under_a_stable_id (__main__.TestDuplicateMerging)
The survivor keeps the earliest id but the LATEST write time. ... ok
test_merge_records_absorbed_ids (__main__.TestDuplicateMerging) ... ok
test_merge_sums_retrieval_count_and_keeps_longest_value (__main__.TestDuplicateMerging)
Retrieval counts add up across a merged cluster. ... ok
test_health_score_is_stable_after_the_second_pass (__main__.TestIdempotency) ... ok
test_maintenance_improves_the_health_score (__main__.TestIdempotency) ... ok
test_second_pass_makes_no_changes (__main__.TestIdempotency) ... ok
test_store_state_is_byte_identical_after_the_second_pass (__main__.TestIdempotency) ... ok
test_log_has_one_line_per_change_with_before_and_after (__main__.TestMaintenanceLog) ... ok
test_normalization_strips_case_and_punctuation (__main__.TestNormalization) ... ok
test_similarity_is_between_zero_and_one (__main__.TestNormalization) ... ok
test_never_retrieved_and_old_is_an_orphan (__main__.TestOrphans) ... ok
test_recent_unread_record_is_not_yet_an_orphan (__main__.TestOrphans) ... ok
test_retrieved_record_is_not_an_orphan (__main__.TestOrphans) ... ok
test_merge_does_not_shorten_a_still_open_belief (__main__.TestRestatementRecency)
A closed copy must not drag an open restatement shut. ... ok
test_recently_restated_ephemeral_is_not_expired_against_the_old_copy (__main__.TestRestatementRecency)
TTL must be measured from the restatement, not the first write. ... ok
test_restated_value_wins_over_the_value_it_replaced (__main__.TestRestatementRecency)
Berlin, then Munich, then Berlin again: Berlin is current. ... ok
test_restatement_listed_first_still_resolves_by_write_time (__main__.TestRestatementRecency)
Same facts, reversed list order. The newest write still wins. ... ok
test_survivor_carries_the_latest_write_time_and_a_stable_id (__main__.TestRestatementRecency) ... ok
test_three_distinct_values_supersede_normally (__main__.TestRestatementRecency)
No duplicates at all. Plain supersession is untouched by the fix. ... ok
test_event_stream_is_deterministic_for_a_fixed_seed (__main__.TestSimulation) ... ok
test_generated_duplicates_are_detectable_as_duplicates (__main__.TestSimulation) ... ok
test_delete_removes_the_record (__main__.TestStoreBackends) ... ok
test_deleting_a_missing_id_is_not_an_error (__main__.TestStoreBackends) ... ok
test_json_round_trip_through_disk (__main__.TestStoreBackends) ... ok
test_sqlite_round_trip (__main__.TestStoreBackends) ... ok
test_chain_of_three_leaves_one_open (__main__.TestSupersession) ... ok
test_older_record_is_closed_at_the_newer_write_time (__main__.TestSupersession) ... ok
test_audit_of_an_aware_store_with_a_naive_clock_does_not_raise (__main__.TestTimezoneAwareClock) ... ok
test_aware_clock_against_a_naive_store_does_not_raise (__main__.TestTimezoneAwareClock) ... ok
test_cli_default_clock_handles_an_aware_store (__main__.TestTimezoneAwareClock) ... ok
test_default_clock_is_timezone_aware_utc (__main__.TestTimezoneAwareClock) ... ok
test_naive_clock_against_an_aware_store_does_not_raise (__main__.TestTimezoneAwareClock) ... ok
test_audit_flags_overdue_records (__main__.TestTtlExpiry) ... ok
test_class_without_ttl_is_never_closed (__main__.TestTtlExpiry) ... ok
test_custom_ttl_policy_is_honoured (__main__.TestTtlExpiry) ... ok
test_fresh_ephemeral_is_left_open (__main__.TestTtlExpiry) ... ok
test_overdue_ephemeral_is_closed_at_its_expiry (__main__.TestTtlExpiry) ... ok

----------------------------------------------------------------------
Ran 48 tests in 0.030s

OK
```

---

## Verbatim: `python3 -B demo_drift.py`

```
==============================================================================
AGENT MEMORY DRIFT SIMULATION
==============================================================================
seed                    : 20260930
simulated days          : 30  (day 1 = 2026-01-01)
entities                : 40
fact classes            : identity, preference, status, ephemeral
similarity threshold    : 0.90
ttl policy (days)       : ephemeral=7, identity=none, preference=none, status=90
maintenance arm cadence : every 7 days

event stream (identical for both arms)
  writes total          : 310
  of which supersessions: 96
  of which duplicates   : 59
  tracked keys          : 155

probe: every known (entity, relation) key is asked 'what is true now?'
       score per probe = matching candidates / candidates believed true
       (expired ground truth: 1.0 only if nothing is returned)

------------------------------------------------------------------------------
 day |   recs     acc |   recs     acc  mnt |    delta
     |    (A)     (A) |    (B)     (B)      |    B - A
------------------------------------------------------------------------------
   1 |     48  100.0% |     48  100.0%      |    +0.0pp
   2 |     51  100.0% |     51  100.0%      |    +0.0pp
   3 |     60   97.5% |     60   97.5%      |    +0.0pp
   4 |     68   96.0% |     68   96.0%      |    +0.0pp
   5 |     77   94.4% |     77   94.4%      |    +0.0pp
   6 |     83   93.2% |     83   93.2%      |    +0.0pp
   7 |     92   90.4% |     68  100.0%  yes |    +9.6pp
   8 |    103   83.3% |     79   91.1%      |    +7.8pp
   9 |    113   83.8% |     89   91.1%      |    +7.4pp
  10 |    123   84.3% |     99   90.4%      |    +6.2pp
  11 |    131   81.9% |    107   87.8%      |    +5.9pp
  12 |    144   81.4% |    120   86.8%      |    +5.4pp
  13 |    152   81.4% |    128   86.3%      |    +4.9pp
  14 |    161   82.0% |     92  100.0%  yes |   +18.0pp
  15 |    172   80.0% |    103   96.5%      |   +16.6pp
  16 |    181   79.2% |    112   94.8%      |   +15.6pp
  17 |    190   75.9% |    121   91.3%      |   +15.4pp
  18 |    200   75.6% |    131   89.5%      |   +13.9pp
  19 |    212   73.6% |    143   86.5%      |   +12.9pp
  20 |    219   73.1% |    150   85.4%      |   +12.4pp
  21 |    227   71.3% |    112   97.5%  yes |   +26.2pp
  22 |    235   70.1% |    120   93.6%      |   +23.5pp
  23 |    242   70.3% |    127   93.0%      |   +22.6pp
  24 |    249   71.2% |    134   92.3%      |   +21.1pp
  25 |    258   71.3% |    143   91.2%      |   +19.9pp
  26 |    269   71.4% |    154   89.0%      |   +17.6pp
  27 |    278   70.6% |    163   88.0%      |   +17.5pp
  28 |    289   69.6% |    129   99.3%  yes |   +29.7pp
  29 |    299   70.5% |    139   98.0%      |   +27.5pp
  30 |    310   70.9% |    150   97.1%      |   +26.2pp
------------------------------------------------------------------------------

==============================================================================
HEADLINE NUMBERS
==============================================================================
                                   no maintenance    maintenance
day 1 retrieval accuracy                   100.0%         100.0%
day 30 retrieval accuracy                   70.9%          97.1%
accuracy change day 1 -> day 30            -29.1pp          -2.9pp
day 30 memory health score                   66.4           90.9
day 30 records stored                         310            251
day 30 records believed true                  310            150
day 30 contradiction pairs                    185              7
day 30 near-duplicate clusters                 66              4
day 30 ttl-overdue records                     42              1
day 30 orphan records                          20              4

maintenance arm applied 213 logged changes across 4 passes.
day-30 accuracy delta   : +26.2 percentage points in favour of maintenance
relative error reduction: 90.0% of the day-30 error removed

------------------------------------------------------------------------------
HEALTH SCORE BREAKDOWN, DAY 30, NO MAINTENANCE
------------------------------------------------------------------------------
========================================================================
MEMORY HYGIENE AUDIT
========================================================================
evaluated at        : 2026-01-30T20:00:00
records             : 310 total / 310 believed true / 0 closed
duplicate threshold : 0.90   orphan grace: 14 days

-- findings (over the 310 believed-true records) --
contradiction pairs : 185  across 53 entity/relation keys  (176 records, 56.8%)
near-dup clusters   : 66  (82 redundant records, 26.5%)
ttl overdue         : 42 records (13.5%)
orphans (never read): 20 records (6.5%)
unbounded, no TTL   : 165 records (53.2%)

-- age distribution (believed-true records) --
  0-7d       68   21.9%
  8-30d     242   78.1%
  31-90d      0    0.0%
  90d+        0    0.0%

-- staleness by fact class --
  class          live  ttl_days   mean_age    max_age   overdue
  ephemeral        49         7      16.68      29.46     85.7%
  identity         44         -      16.87      29.46      0.0%
  preference      121         -      16.41      29.46      0.0%
  status           96        90      15.86      29.46      0.0%

-- memory health score --
  formula: score = 100 - (35*contradiction_rate + 20*duplicate_rate + 25*ttl_overdue_rate + 12*orphan_rate + 8*unbounded_rate); every rate = affected_live_records / live_records
  component          weight     rate    penalty
  contradictions       35.0   0.5677      19.87
  duplicates           20.0   0.2645       5.29
  ttl_overdue          25.0   0.1355       3.39
  orphans              12.0   0.0645       0.77
  unbounded             8.0   0.5323       4.26
  total penalty                           33.58

  MEMORY HEALTH SCORE: 66.4 / 100
========================================================================

------------------------------------------------------------------------------
HEALTH SCORE BREAKDOWN, DAY 30, MAINTENANCE EVERY 7 DAYS
------------------------------------------------------------------------------
========================================================================
MEMORY HYGIENE AUDIT
========================================================================
evaluated at        : 2026-01-30T20:00:00
records             : 251 total / 150 believed true / 101 closed
duplicate threshold : 0.90   orphan grace: 14 days

-- findings (over the 150 believed-true records) --
contradiction pairs : 7  across 7 entity/relation keys  (14 records, 9.3%)
near-dup clusters   : 4  (5 redundant records, 3.3%)
ttl overdue         : 1 records (0.7%)
orphans (never read): 4 records (2.7%)
unbounded, no TTL   : 88 records (58.7%)

-- age distribution (believed-true records) --
  0-7d       64   42.7%
  8-30d      86   57.3%
  31-90d      0    0.0%
  90d+        0    0.0%

-- staleness by fact class --
  class          live  ttl_days   mean_age    max_age   overdue
  ephemeral         8         7       4.46       8.46     12.5%
  identity         32         -      14.80      29.46      0.0%
  preference       56         -       9.78      29.46      0.0%
  status           54        90      11.61      29.46      0.0%

-- memory health score --
  formula: score = 100 - (35*contradiction_rate + 20*duplicate_rate + 25*ttl_overdue_rate + 12*orphan_rate + 8*unbounded_rate); every rate = affected_live_records / live_records
  component          weight     rate    penalty
  contradictions       35.0   0.0933       3.27
  duplicates           20.0   0.0333       0.67
  ttl_overdue          25.0   0.0067       0.17
  orphans              12.0   0.0267       0.32
  unbounded             8.0   0.5867       4.69
  total penalty                            9.11

  MEMORY HEALTH SCORE: 90.9 / 100
========================================================================

stores written to .demo_runs/ for inspection.
```

---

## Live round trip against Agent Platform Memory Bank

The first measurement in this package taken against a real Google service rather than a
simulation. Everything above this section is a seeded local simulation and is labelled as
such. This is not.

Scope, because it matters: one run, on 7 October 2026, with
`google-cloud-aiplatform 2.4.0` on the `vertexai` surface. That surface is deprecated in
favour of `agentplatform`. The migration to the new client is NOT covered here.

### Why this needed doing

The first release of `VertexMemoryBankStore` could not write to Memory Bank at all. Every
write failed:

```
TypeError: AgentEngines.create_memory() got an unexpected keyword argument 'metadata'
```

The real signature is `create_memory(*, name, fact, scope, config=None)`. Metadata is a
field of the config type, and each value is a typed `MemoryMetadataValue` carrying
`string_value`, `bool_value`, `double_value` or `timestamp_value`, not a bare string.

It shipped that way because the fake transport in the unit tests accepted any keyword
argument. 35 tests passed by asserting that the adapter called the fake the way the
adapter calls the fake. Since no write ever succeeded, the read path had never been
exercised on adapter-written data either.

The fake is now strict: keyword-only parameters copied from the real SDK, config dicts
checked against the real config field names, typed metadata enforced, and responses
shaped like the real ones. Where the SDK is importable, two tests compare the fake's
signatures against the installed SDK in a subprocess. Running the OLD adapter against the
NEW fake fails in exactly the way the live service failed:

```
FAILED (failures=6, errors=10, skipped=2)
8x TypeError: FakeMemoryBankClient.create_memory() got an unexpected keyword argument 'metadata'
1x TypeError: FakeMemoryBankClient.list_memories() got an unexpected keyword argument 'page_token'
AssertionError: "{'string_value': 'user:1042'}" != 'user:1042'
```

That first line is the live error, reproduced offline. Test count is now 48, of which 46
pass and 2 skip without the SDK, and all 48 pass with it installed.

### The run

Verbatim, with the project number masked.

```
memory-hygiene VertexMemoryBankStore, live round trip
run at (UTC)        : 2026-10-07T13:45:17+00:00
google-cloud-aiplatform 2.4.0 | python 3.12.3
location            : us-central1
throwaway engine    : projects/<project-number>/locations/us-central1/reasoningEngines/2929697385598681088

== construction: injected client
  update() x2, then all_records(): 2 records
     ('user:ada', 'employer', 'Acme', 'status', '2026-09-07T13:45:20', '2026-11-06T13:45:20', None, ['old-1', 'old-2'], None, None)
     ('user:ada', 'timezone', 'Europe/Berlin', 'preference', '2026-08-08T13:45:20', None, 's-101', [], None, None)
  retrieve('Which timezone is Ada in?', top_k=1): [('timezone', 'Europe/Berlin')]
  audit_store health score: 96.0
  update() of an existing record (delete then create):
     ('user:ada', 'employer', 'Acme', 'status', '2026-09-07T13:45:20', '2026-11-06T13:45:20', None, ['old-1', 'old-2'], None, None)
     ('user:ada', 'timezone', 'Asia/Jerusalem', 'preference', '2026-08-08T13:45:20', None, 's-101', [], None, None)
  generate() then all_records(), generated memory read back:
     ('user:live-check-a', 'fact', 'Ada prefers Python.', 'status', '2026-10-07T13:45:48.646422', None, None, [], None, None)
  delete() every record, then all_records(): 0 records

== construction: default lazy construction
  update() x2, then all_records(): 2 records
     ('user:ada', 'employer', 'Acme', 'status', '2026-09-07T13:45:57', '2026-11-06T13:45:57', None, ['old-1', 'old-2'], None, None)
     ('user:ada', 'timezone', 'Europe/Berlin', 'preference', '2026-08-08T13:45:57', None, 's-101', [], None, None)
  retrieve('Which timezone is Ada in?', top_k=1): [('timezone', 'Europe/Berlin')]
  audit_store health score: 96.0
  update() of an existing record (delete then create):
     ('user:ada', 'employer', 'Acme', 'status', '2026-09-07T13:45:57', '2026-11-06T13:45:57', None, ['old-1', 'old-2'], None, None)
     ('user:ada', 'timezone', 'Asia/Jerusalem', 'preference', '2026-08-08T13:45:57', None, 's-101', [], None, None)
  generate() then all_records(), generated memory read back:
     ('user:live-check-b', 'fact', 'Ada prefers Python.', 'status', '2026-10-07T13:46:20.893707', None, None, [], None, None)
  delete() every record, then all_records(): 0 records

cleanup: engine deleted, get() now raises ClientError
result: PASS
```

Tuple columns: `entity`, `relation`, `value`, `fact_class`, `written_at`, `valid_to`,
`source_session`, `merged_from`, `retrieval_count`, `last_retrieved_at`.

Note the last two columns are `None` on every row. That is not a bug in the adapter. Memory
Bank exposes no retrieval counter and no last-accessed timestamp, so those fields cannot be
populated from it, which is why the orphan metric in this package cannot be computed
against Memory Bank today.

An earlier draft of these notes reported a health score of 92.0. That came from a
one-record scratch run with unfinished code and is withdrawn. The figure is 96.0, from the
transcript above.

### Four fixes this produced

1. **Write.** Metadata moves into `config.metadata`, every value typed as `string_value`.
2. **Read.** Typed values are unwrapped on the way back, handling both plain dicts and SDK
   objects.
3. **Paging.** `page_token` belongs inside config. With the real SDK the loop makes exactly
   one call, because the SDK's pager fetches every page itself.
4. **Operation name.** `update()` recorded the operation's name as if it were the memory's.
   `create_memory` returns an operation with the memory under `response`. This was harmless
   because lookups fell back to a listing, but it was wrong.
