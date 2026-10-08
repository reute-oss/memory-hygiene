# VERIFICATION

> **Read Addendum 2 at the end of this file first, then Addendum 4.** This
> document is an append-only audit log. Addendum 4 records the rename of the
> Memory Bank adapter, so file and class names above it are the old ones. Sections above Addendum 2 record what was true at the
> time they were written, including headline numbers that a later correctness
> fix has since changed. Where anything here conflicts with Addendum 2,
> Addendum 2 is authoritative.


Independent assembly and verification of the `memory-hygiene` package.
Everything below was observed directly. Nothing is estimated or carried over
from prior documentation.

- Date of run: 2026-10-07
- Python: 3.9.6 (CPython, Apple Clang toolchain)
- Platform: macOS 15.8.1, Darwin 24.6.0, arm64
- Third party packages installed: none. No numpy, no pandas, no pytest,
  no Google SDK. No network access.
- Test runner: `python3 <file>.py` (stdlib `unittest`), not pytest.

## Headline result

All functional checks PASS. All six compliance checks PASS. All five
STEP 3 headline numbers MATCH.

Two documentation defects were found. Both are wrong commands in the prose,
not wrong numbers and not code faults. They are listed under
"Defects found". No source file required fixing, so no file was modified.

## 1. File tree

Assembled by copying only the files named in the specification. The result
is exactly the required 16 files, no more.

```
memory-hygiene/
  README.md
  RESULTS.md
  LICENSE
  NOTICE
  DISCLAIMER.md
  GAPS.md
  pyproject.toml
  memory_hygiene_store.py
  memory_hygiene_audit.py
  memory_hygiene_maintain.py
  memory_hygiene_vertex.py
  demo_drift.py
  test_hygiene.py
  test_vertex_adapter.py
  examples/adk_agent.py
  notebooks/memory_hygiene_colab.ipynb
```

File count: 16. Plus this `VERIFICATION.md`, 17.

### Absence of `demo_decay.py`

Explicitly confirmed. A recursive search of the whole tree for `demo_decay*`
returned 0 matches. There is no file by that name at any depth.

Note: running the demo and the test suites creates two hidden working
directories, `.demo_runs/` and `.scratch/`. These are runtime byproducts, not
source. They are absent from the delivered tree, which was built by copying
only the 16 source files.

## 2. Test and demo execution

### `python3 test_hygiene.py`

```
Ran 37 tests in 0.028s

OK
```

37 tests, 0 failures, 0 errors. Final status line: `OK`.

### `python3 test_vertex_adapter.py`

```
Ran 35 tests in 0.003s

OK
```

35 tests, 0 failures, 0 errors. Final status line: `OK`.

Combined: 72 tests, all passing.

### `python3 demo_drift.py`

Exit code 0. Clean stderr. 163 lines of stdout covering the simulation
parameters, the 30 day two arm table, a headline summary block, and two full
health score breakdowns.

### Determinism

The demo was run a second time to a separate file and the two captures were
compared with `cmp`.

- `cmp run1.txt run2.txt` exited 0 with no output. Byte identical.
- Both files: 163 lines.
- MD5 of both: `846f7a4594fbd8d2ec32010979e86da9`.

Determinism holds. Result: CLEAN.

### Lazy SDK import

`python3 -c "import memory_hygiene_vertex"` succeeded, exit code 0, no
traceback.

This is a meaningful check here because the SDK really is absent. Both of
these raise `ModuleNotFoundError` in this environment:

- `import google.cloud.aiplatform`
- `import vertexai`

So the module imports cleanly with no Google SDK present. The import is
genuinely lazy. Result: PASS, no bug.

## 3. Headline number checks

**SUPERSEDED. Do not quote this section as current.** It records the original
verification pass against the pre-fix `maintain` code. The correctness fix in
Addendum 2 moved three of these numbers. The maintained-arm figures below
(96.5%, 90.7, 87.8%) are what that older build really printed and are kept
verbatim as the historical record, not rewritten. The current, re-measured
values are 97.1% day-30 maintained accuracy, 90.9 maintained health score and
90.0% relative error reduction. See "Addendum 2: the headline numbers moved,
and improved". The no-maintenance figures (70.9%, 66.4) are unchanged and still
current.

Checked against the demo stdout generated at the time of that pass.

| # | Claim | Expected | Observed | Result |
|---|-------|----------|----------|--------|
| 1 | Day 30 retrieval accuracy, no maintenance | 70.9% | 70.9% | MATCH |
| 2 | Day 30 retrieval accuracy, maintenance | 96.5% | 96.5% | MATCH |
| 3 | Day 30 memory health score | 66.4 and 90.7 | 66.4 and 90.7 | MATCH |
| 4 | Day 30 contradiction pairs | 185 and 7 | 185 and 7 | MATCH |
| 5 | Relative error reduction | 87.8% | 87.8% | MATCH |

All five MATCH. Source lines from the run:

```
day 30 retrieval accuracy                   70.9%          96.5%
day 30 memory health score                   66.4           90.7
day 30 contradiction pairs                    185              7
relative error reduction: 87.8% of the day-30 error removed
```

## 4. Cross-check of numbers in README.md and RESULTS.md

Every numeric token in both documents was extracted and tested against the
fresh demo stdout. The substantive simulation figures all appear. The tokens
that do not appear verbatim fall into four groups, none of which is a
fabricated result.

**Group A, not demo output by nature.** Environment and tooling strings:
`3.9`, `15.7`, `64` (from a platform line), `256` (from `shasum -a 256`),
and the SHA256 digest fragments on RESULTS.md lines 33, 34, 42 and 101.
Digests of a previous run are not expected to recur as numbers in stdout.

**Group B, test metadata.** `37` and `0.028` are the test count and test
duration. These come from the test suite, not the demo. Both were
independently confirmed correct in section 2: the suite does report
`Ran 37 tests in 0.028s`.

**Group C, illustrative prose.** README lines 194 and 195 use `85` as a
hypothetical score while explaining that the score is not calibrated. It is
not a claimed measurement.

**Group D, a different experiment.** RESULTS.md lines 87 to 104 quote
`94.6` and `134`, which do not appear in the demo output. These are not
errors. That passage documents a separate one shot maintenance run over the
drifted store, not the every 7 days arm of the demo. To avoid leaving this
unresolved, the experiment was reproduced from the demo's own
`store_no_maintenance.json` output.

Reproduced exactly:

```
changes             : 242
  absorb_duplicate     82
  close_superseded     73
  merge_duplicate      66
  ttl_close            21
records deleted     : 82
live records        : 310 -> 134
contradiction pairs : 185 -> 0
near-dup clusters   : 66 -> 0
ttl overdue         : 42 -> 0
health score        : 66.4 -> 94.6
```

Every figure in that RESULTS.md block is confirmed: 66.4 to 94.6, 310 to 134,
185 to 0, 66 to 0, 42 to 0, 242 changes, 82 deleted.

Two further RESULTS.md claims in the same section were also reproduced:

- Idempotency. A second identical `--apply` reported `changes: 0`, left the
  store at `134 -> 134` and `94.6 -> 94.6`, and appended 0 new log lines
  (242 before, 242 after).
- CI gate. `--fail-below 90` exited 0 against the 94.6 store;
  `--fail-below 99` exited 1.

Conclusion for STEP 4: no number in README.md or RESULTS.md is unaccounted
for, and none is fabricated.

## 5. Compliance audit

Scanned across the whole tree.

| Check | Result | Evidence |
|-------|--------|----------|
| a. Disclaimer verbatim | PASS | Exact character for character match in all 6 locations checked |
| b. No Unicode dashes | PASS | 0 occurrences of U+2010 to U+2015 and U+2212 |
| c. No forbidden vendor terms | PASS | 0 case insensitive matches in any file |
| d. LICENSE Apache 2.0 | PASS | 201 lines, full text, correct copyright |
| e. pyproject dependencies | PASS | `dependencies = []`, `vertex` extra present |
| f. Notebook integrity | PASS | Parses via `json.load()`, all code cells have empty outputs |

### (a) Disclaimer, verbatim in all required places

Compared character for character against the reference string. Exact
substring match, not a normalized or fuzzy match, in every location:

| Location | Exact match |
|----------|-------------|
| `README.md` | yes |
| `DISCLAIMER.md` | yes |
| `memory_hygiene_vertex.py` (top of file, within first 1600 chars) | yes |
| notebook cell index 1 (markdown cell) | yes |
| `test_vertex_adapter.py` | yes |
| `examples/adk_agent.py` | yes |

All four required places plus both additional files requested. PASS.

### (b) Dash characters

Every file was read as UTF-8 and scanned codepoint by codepoint for
U+2010, U+2011, U+2012, U+2013, U+2014, U+2015 and U+2212. Zero hits. No
file, no line. ASCII hyphens are present and are permitted. PASS.

This was done in Python rather than with `grep -P`, because the BSD grep on
this platform cannot be relied on for `\x{...}` classes and a silent false
negative was not acceptable.

### (c) Forbidden vendor terms

Case insensitive search for the two prohibited vendor names across all files.
Zero occurrences. PASS.

### (d) LICENSE

- 201 lines, 11340 characters.
- Contains `Apache License` and `Version 2.0, January 2004`.
- Contains `1. Definitions`, `9. Accepting Warranty`,
  `END OF TERMS AND CONDITIONS` and the `APPENDIX: How to apply` section.
- Contains `Copyright 2026 Google LLC`.

Full official text with the correct copyright line. PASS.

### (e) pyproject.toml

- `dependencies = []`, with a comment stating the package is pure standard
  library by design and that no runtime dependencies should be added.
- `[project.optional-dependencies]` declares a `vertex` extra containing
  `google-cloud-aiplatform` and `google-genai`.
  **SUPERSEDED, see Addendum 4:** the extra is now `memorybank`, containing
  `google-cloud-agentplatform; python_version >= '3.10'`.

No runtime dependencies, `vertex` optional extra present. PASS.

### (f) Notebook

- Loads successfully with `json.load()`.
- 16 cells total, 7 of them code cells.
- Code cells with a non-empty `outputs` list: 0.
- Code cells with a non-null `execution_count`: 0.

No fabricated or stored results anywhere in the notebook. PASS.

## Defects found

Two documentation defects. Both are incorrect invocation commands in prose.
Neither affects the code, the tests or any reported number. Neither was
edited, because nothing blocked verification and silently rewriting the
documents would hide the finding.

1. **RESULTS.md, line 83.** Documents the command
   `python3 -m memory_hygiene.maintain --store work.json --apply`. This
   fails with `ModuleNotFoundError: No module named 'memory_hygiene'`. The
   package ships flat modules, not a `memory_hygiene` package. The working
   command is `python3 memory_hygiene_maintain.py --store work.json --apply`.
   The numbers that passage reports are correct; only the command is wrong.

2. **README.md, line 168.** Documents
   `python3 -m unittest discover -s tests -v   # 37 tests`. This fails with
   `ImportError: Start directory is not importable: 'tests'`. There is no
   `tests/` directory; the test files sit in the root. The working commands
   are `python3 test_hygiene.py` and `python3 test_vertex_adapter.py`. The
   count of 37 is correct for `test_hygiene.py`.

## Limitations

Stated plainly rather than left implied.

- No live Vertex AI call was made. The Google SDK is not installed and there
  is no network. The Vertex adapter is verified only through its 35 unit
  tests and the lazy import check. Its behaviour against the real service is
  unverified here.
- The notebook was validated structurally, as JSON and for empty outputs. Its
  cells were not executed, since execution would require the SDK and network.
- `pyproject.toml` was read and checked by inspection. No build or install was
  attempted, as `pip install` cannot run in this environment.
- Determinism was confirmed across two runs on one machine, one Python build
  and one platform. Cross platform determinism is not established by this run.

---

## Addendum: post-verification fixes

Recorded after the verification run above, so the audit trail stays honest.

The verification pass reported two broken command invocations. A broader sweep
found **nine** occurrences of the same two root causes, not two. The pass only
flagged the two it happened to execute. All of them are now fixed.

Root cause 1: the package ships flat modules, so there is no importable
`memory_hygiene` package. Every `python3 -m memory_hygiene.<x>` invocation
fails with `ModuleNotFoundError`.

Root cause 2: there is no `tests/` directory, so
`python3 -m unittest discover -s tests` fails with
`Start directory is not importable`.

Fixed, 8 edits across 3 files (one occurrence was a prefix of a longer rule):

| File | Was | Now |
|---|---|---|
| README.md | `python3 -m memory_hygiene.audit --store mystore.json` | `python3 memory_hygiene_audit.py --store mystore.json` |
| README.md | `python3 -m memory_hygiene.maintain --store mystore.json` | `python3 memory_hygiene_maintain.py --store mystore.json` |
| README.md | `python3 -m memory_hygiene.maintain ... --apply --log maint.jsonl` | `python3 memory_hygiene_maintain.py ... --apply --log maint.jsonl` |
| README.md | `python3 -m memory_hygiene.audit ... --json --fail-below 80` | `python3 memory_hygiene_audit.py ... --json --fail-below 80` |
| README.md | `python3 -m unittest discover -s tests -v` | `python3 test_hygiene.py` |
| RESULTS.md | `python3 -m memory_hygiene.maintain --store work.json --apply` | `python3 memory_hygiene_maintain.py --store work.json --apply` |
| RESULTS.md | `python3 -m unittest discover -s tests -v` | `python3 test_hygiene.py` |
| test_hygiene.py | `python3 -m unittest discover -s tests -v` (docstring) | `python3 test_hygiene.py` |

Residual scan for `-m memory_hygiene.` and `unittest discover` across the tree:
0 hits, excluding this report and the defect description above, which quote the
broken forms deliberately.

### Every corrected command was executed, not assumed

Run against `.demo_runs/store_no_maintenance.json`, a real store produced by
`demo_drift.py`, not a hand-written fixture.

| Command | Observed |
|---|---|
| `python3 memory_hygiene_audit.py --store <store>` | runs, MEMORY HEALTH SCORE: 55.2 / 100 |
| `python3 memory_hygiene_maintain.py --store work.json` | dry run, health 55.2 -> 87.8, ttl overdue 145 -> 0 |
| `python3 memory_hygiene_maintain.py --store work.json --apply --log maint.jsonl` | applies, same deltas, log written |
| `python3 memory_hygiene_audit.py --store work.json --json --fail-below 80` | exit 0, correct, post-maintenance score 87.8 is above the gate |
| `python3 test_hygiene.py` | OK |

### One article defect found in the same sweep

Not a package fault, but it would have shipped. The article's `MemoryRecord`
code block did not match the real dataclass. It showed `predicate`,
`superseded_by` and `last_read_at`. The real fields are `relation`,
`last_retrieved_at` and `retrieval_count`, plus `written_at`, `source_session`
and `merged_from`. `superseded_by` does not exist anywhere in the package, yet
the article named it as one of the two fields that carry temporal information.
A reader copying that snippet would have got an error. The article snippet now
matches the dataclass exactly.

---

## Addendum 2: external review, correctness fix, and new numbers

Added after an independent reviewer with network access examined the package.
Everything in the sections above predates this and is superseded where it conflicts.

### A real correctness bug was found and fixed

The reviewer found, and an independent reproduction confirmed, that
`_merge_duplicates` could leave the agent believing a **superseded** value. This
is the exact failure the package exists to prevent, so it is worth stating plainly.

Reproduction, one entity and relation:

```
Berlin  written 2026-01-01
Munich  written 2026-01-05
Berlin  written 2026-01-10
```

"Newer is right" makes Berlin current. Before the fix the tool left **Munich**
open and closed Berlin.

Cause: `run_maintenance` runs `_merge_duplicates`, then `_close_superseded`,
then `_apply_ttl`. The merge collapsed a cluster into its **earliest** copy,
destroying the recency of the restated fact. Supersession then compared
Berlin@Jan1 against Munich@Jan5 and correctly chose Munich. Each step was right
on its own. The ordering made the outcome wrong. No test covered it, which is
why 37 passing tests did not catch it.

Fix, inside `_merge_duplicates` rather than by reordering: the survivor keeps
its existing `id` but takes the **latest** `written_at` and `valid_from` in the
cluster, the **sum** of `retrieval_count`, and the **max** `last_retrieved_at`.
For `valid_to` the survivor takes the latest end, where an open record counts as
later than any closed one, because merging is a storage decision and not new
evidence that a belief ended.

A second defect shared the root cause: TTL expiry was computed from the earliest
copy, so a recently restated short-lived fact could be expired against a stale
timestamp.

### Two pre-existing tests were rewritten, not weakened

They asserted the old behaviour and were in direct logical contradiction with
the new regression tests.

- `test_merge_keeps_earliest_written_at` became
  `test_merge_keeps_latest_written_at_under_a_stable_id`.
- `test_merge_keeps_highest_retrieval_count_and_longest_value` became
  `test_merge_sums_retrieval_count_and_keeps_longest_value`. Counts now sum,
  because a cluster is one fact asserted several times, and a max rule can mark
  a genuinely used fact as an orphan and expire it.

Both carry docstrings recording why they changed.

### The headline numbers moved, and improved

| Metric | Addendum 1 | Now |
|---|---|---|
| Day 30 accuracy, no maintenance | 70.9% | 70.9% unchanged |
| Day 30 accuracy, maintained | 96.5% | **97.1%** |
| Day 30 accuracy change, maintained | -3.5pp | **-2.9pp** |
| Day 30 health score, maintained | 90.7 | **90.9** |
| Day 30 records stored, maintained | 252 | **251** |
| Day 30 orphan records, maintained | 6 | **4** |
| Relative error reduction | 87.8% | **90.0%** |

The no-maintenance column is unchanged throughout, which is the expected
signature of a fix that only touches the maintenance pass. Restated facts now
keep their real recency, so maintenance performs better.

Determinism holds: two consecutive demo runs are byte-identical.

### Test count

**48 tests, all passing**, up from 37. The 11 new tests are
`TestRestatementRecency` (6) and `TestTimezoneAwareClock` (5). Both classes
failed before their fixes and pass after, which is the only reason to trust them.

### A process error worth recording

During this pass the documentation refresh was handed a **stale**
`memory_hygiene_audit.py`, because only `maintain.py` and `store.py` were
propagated into its working tree. It observed 4 genuine timezone errors and
honestly recorded "48 tests, 44 pass, 4 error" in README and RESULTS. With the
correct file set the suite is clean. Those claims were corrected against a live
run, and the verbatim test block was regenerated rather than hand-edited.

The lesson is the same one that produced the earlier stale-zip review: when a
fix spans several files, propagate all of them before anyone measures anything.

### Still outstanding

- `pyproject.toml` declares no `py-modules`, so a build produces a
  metadata-only wheel with no code in it.
- The notebook instructs `pip install memory-hygiene[vertex]`, but nothing is
  published to PyPI. It should install from the clone.
- Four `from memory_hygiene.…` import examples in README assume a package
  layout that does not exist in this flat tree.
- The maintenance change log is written after the store is rewritten, so a
  crash partway through leaves changes on disk with no log entries. That
  undercuts the reversibility claim.
- Audit and maintain disagree on one edge case: a record past its expiry that
  already carries a future end date is flagged by audit and never acted on.
- The Memory Bank adapter was verified against a live Memory Bank on 7 October 2026, on one run with google-cloud-aiplatform 2.4.0. The first release could not
  write at all: metadata was passed as a top-level argument when the real API
  nests it inside config as typed values. The original fake accepted any
  argument, so 35 tests passed against a bug the live service rejects
  outright. The fake is now strict. The agentplatform migration is NOT yet
  verified live.

## Addendum 3: runnable-documentation pass

Scope was the documentation only. No change was made to `memory_hygiene_store.py`,
`memory_hygiene_audit.py`, `memory_hygiene_maintain.py` or `demo_drift.py`, so no
measured result moves in this pass.

### What was broken

An external reviewer's `check_docs.py` executes every runnable snippet in
README.md and the Colab notebook. It reported **6 FAILED** at the start of this
pass:

```
notebook cell 5    FAILED  TypeError: __init__() got an unexpected keyword argument 'record_id'
notebook cell 6    FAILED  TypeError: __init__() got an unexpected keyword argument 'stale_after_days'
notebook cell 8    FAILED  TypeError: __init__() got an unexpected keyword argument 'stale_after_days'
notebook cell 9    FAILED  NameError: name 'store' is not defined
README block 2     FAILED  ModuleNotFoundError: No module named 'memory_hygiene'
README block 3     FAILED  ModuleNotFoundError: No module named 'memory_hygiene'
```

Two root causes. The notebook was written against an API that does not exist:
`record_id`, `stale_after_days`, `duplicate_similarity`, `resolve_contradictions`,
`delete_orphans`, `store.reset()`, `store.register_entities()`,
`store.list_records()` and `result.actions` are all invented names. And README
blocks 2 and 3 imported `from memory_hygiene.<module>`, a package layout this
flat tree does not have.

### The real API, introspected not guessed

Every parameter now written in the docs appears in this output:

```
MemoryRecord fields:
   id str
   entity str
   relation str
   value str
   written_at datetime
   valid_from Optional[datetime]
   valid_to Optional[datetime]
   fact_class str
   last_retrieved_at Optional[datetime]
   retrieval_count int
   source_session Optional[str]
   merged_from List[str]
AuditConfig: (self, now, duplicate_threshold=0.9, orphan_grace_days=14.0, ttl_policy=<factory>)
MaintenancePolicy: (self, now, ttl_policy=<factory>, duplicate_threshold=0.9,
                    orphan_grace_days=14.0, close_superseded=True,
                    merge_duplicates=True, apply_ttl=True)
audit_store: (store, config) -> AuditReport
run_maintenance: (store, policy, dry_run=True, log_path=None) -> MaintenanceResult
JsonMemoryStore: (self, path, write_through=True)
store methods: add, add_many, all_records, delete, get, live_records,
               load, ordered_records, save, update
```

`MaintenanceResult.change_count` is a property, not a method.

### Changes

- **Notebook cell 5** rebuilt against the real `MemoryRecord` fields and the real
  store API. Nine handwritten records that between them trip every detector. The
  comment marking the data as synthetic is kept. The cell is now re-runnable: it
  clears existing records before adding, instead of calling a `reset()` that does
  not exist.
- **Notebook cells 6 and 8** now use `duplicate_threshold`, `orphan_grace_days`
  and `ttl_policy`, and for the policy also `close_superseded`, `merge_duplicates`
  and `apply_ttl`.
- **Notebook cell 9** works now that cell 5 defines `store`. Switched to
  `store.all_records()`, `store.live_records(now)` and `second_pass.change_count`.
- **Notebook cell 4** redefined "orphan". It claimed a record whose owning entity
  was deleted. The package has no notion of entity deletion. `find_orphans`
  selects `retrieval_count == 0 and last_retrieved_at is None and age_days >= grace`,
  so an orphan is a record that has never been read back. The fifth reported
  signal, `unbounded`, is now named here too.
- **Notebook cell 7** rewritten. It promised a "suggested action" on each finding
  and a per-record reason string. `AuditReport` has neither. It now describes the
  four sections `render()` actually prints, points at the id-bearing fields on the
  report object and at `to_dict()`, and names the real knobs.
- **Notebook cell 13** was compile-only but documented the same invented API. It
  now passes a real `now` to `AuditConfig` and `MaintenancePolicy`.
- **Notebook cell 12 and `memory_hygiene_vertex.py`** install hint changed from
  the unpublished `pip install memory-hygiene[vertex]` to `pip install -e ".[vertex]"`
  from the clone.
- **README blocks 2 and 3** imports flattened to `memory_hygiene_audit`,
  `memory_hygiene_maintain` and `memory_hygiene_store`. A sweep of the whole README
  found no remaining `memory_hygiene.` dotted import and no `-m memory_hygiene.`
  invocation.
- **`pyproject.toml`** gained a `[tool.setuptools]` section listing all five
  `py-modules`.
- Notebook cell 1, the disclaimer, is byte-identical. Verified by sha256 before and
  after: `63086f44d3ff1f4710fab867c56e6b4795008158869c97c18f4f7ca0bcc2704f`.
- Every code cell's `outputs` is empty and `execution_count` is null. No output was
  fabricated.

### Result

```
notebook cell 3              SKIPPED  clone cell, --local mode
notebook cell 5              OK
notebook cell 6              OK
notebook cell 8              OK
notebook cell 9              OK
notebook cell 11             OK
notebook cell 13             SKIPPED  marked NOT RUN, compiled only
README block 1 (bash)        OK
README block 2 (python)      OK
README block 3 (python)      OK
README block 4 (untagged)    SKIPPED  not code
README block 5 (bash)        OK

SUMMARY: 9 OK, 0 FAILED, 3 SKIPPED
```

No regression. `test_hygiene.py` ran 48 tests, OK. `test_vertex_adapter.py` ran 35
tests, OK. `demo_drift.py` still prints day 30 accuracy 70.9% and 97.1%, health
66.4 and 90.9, relative error reduction 90.0%.

### Stale-number sweep

Every occurrence of 96.5, 87.8 and 90.7 in the tree was checked rather than
replaced on sight.

- `RESULTS.md` lines 252 and 256 are **correct**. They are per-day rows of the
  simulation table, not headline figures: day 11 maintained is 87.8% and day 15
  maintained is 96.5% in the current demo output. Left alone.
- `VERIFICATION.md` section 3 is **stale** and now carries a SUPERSEDED banner
  pointing at Addendum 2. Its observed values were not rewritten, because that
  older build really did print them and overwriting them would invent a run.
- `VERIFICATION.md` lines 429 to 434 are the Addendum 2 before/after correction
  table. The old values belong there by design. Left alone.
- `VERIFICATION.md` lines 352 and 354 quote health 55.2 -> 87.8 on a `work.json`
  that is not in the tree. **Unverifiable**: the store is gone, so this was neither
  confirmed nor changed. Flagged rather than guessed.
- README.md and RESULTS.md headline figures are already 70.9%, 97.1%, 66.4, 90.9
  and 90.0%. Correct.

### Still outstanding

Closed by this pass: the `py-modules` gap, the PyPI install hint, and the four
dotted README imports.

Still open:

- The maintenance change log is written after the store is rewritten, so a crash
  partway through leaves changes on disk with no log entries. That undercuts the
  reversibility claim.
- Audit and maintain disagree on one edge case: a record past its expiry that
  already carries a future end date is flagged by audit and never acted on.
- The Memory Bank adapter was verified against a live Memory Bank on 7 October 2026, on one run with google-cloud-aiplatform 2.4.0. See Addendum 3.
- `VERIFICATION.md` lines 352 and 354 cannot be re-measured without `work.json`.

## Addendum 4: Agent Platform SDK, rename, Python floor

Recorded on 7 October 2026 in the repository, which is authoritative from here.
Commit 2 moved the adapter to the Agent Platform SDK. Commit 3 renamed it and
fixed the documentation listed below. Nothing above this addendum was rewritten
except the SUPERSEDED marker in section 5(e).

### Rename

| before | after |
|---|---|
| `memory_hygiene_vertex.py` | `memory_hygiene_memorybank.py` |
| `VertexMemoryBankStore` | `MemoryBankStore` |
| constructor argument `agent_engine_id` | `memory_bank` |
| property `engine_name` | `memory_bank_name` |
| `test_vertex_adapter.py` | `test_memorybank_adapter.py` |
| extra `vertex` | `memorybank` |

No compatibility alias. Nothing was published and the repository was private.
`examples/adk_agent.py` keeps `agent_engine_id` where it is an argument of
ADK's own `VertexAiMemoryBankService`, which is not this package's API.

### SDK surface

The adapter calls the nested Memory Bank verbs (`create`, `list`, `retrieve`,
`delete`, `generate`). The flat `*_memory` methods are deprecated on both SDKs.
Default construction prefers `agentplatform.Client().memory_banks.memories` and
falls back to `vertexai.Client().agent_engines.memories`, logging which surface
it took. A bank made by `memory_banks.create()` carries a `reasoningEngines/`
resource name, so the `memory_bank` argument accepts either kind of parent.

### Packaging

- `py-modules` lists the four library modules. `demo_drift` was removed: it is a
  repository script, and installing it put it, and the `.demo_runs/` it writes,
  into site-packages.
- `requires-python` stays `>=3.9`, because the core is standard library only.
- The `memorybank` extra is `google-cloud-agentplatform; python_version >= '3.10'`.
  Both Google SDKs declare Python 3.10 or newer. With this marker, pip on 3.9
  skips the SDK without an error rather than refusing, and the adapter's
  missing SDK message says that 3.10 is required.

### Test count

`test_memorybank_adapter.py`: 53. Without an SDK, 49 pass and 4 skip. With
google-cloud-agentplatform 2.3.0 alone, 51 pass and 2 skip. With
google-cloud-aiplatform 2.4.0, which ships both SDK modules, all 53 pass.
`test_hygiene.py`: 48, unchanged.

### Live verification, Agent Platform SDK

Run on 7 October 2026 with google-cloud-agentplatform 2.3.0 alone, on a bank
made by `memory_banks.create()`, with the renamed code: injected client and
lazy default construction both PASS, audit 96.0. Transcript in `RESULTS.md`.
The older surface, an injected `vertexai.Client` on google-cloud-aiplatform
2.4.0, also passed after the move and before the rename.

One attempt failed before any adapter code ran: `memory_banks.create()` raised
`500 INTERNAL` while polling its operation. The bank it had started was found,
deleted and confirmed gone, and the retry passed. Recorded because a clean
transcript should not hide a failed attempt.

### Documentation fixed in commit 3

- `GAPS.md`: adapter test count 48 corrected to 53. The "never run against a live
  Memory Bank" status line, which contradicted the line above it, replaced with
  the verification record. Python note added.
- `README.md`: adapter tests added to "Running the tests", Python versions and
  the Memory Bank extra documented.
- `RESULTS.md`: verification scope widened to both SDKs, the Agent Platform
  transcript added verbatim. The earlier transcript is untouched.
- Notebook cells 12 to 14 and `examples/adk_agent.py`: new names, the extra, the
  Python floor and the widened verification scope.

### Still outstanding

- The maintenance change log is written after the store is rewritten, so a crash
  partway through leaves changes on disk with no log entries.
- Audit and maintain disagree on one edge case: a record past its expiry that
  already carries a future end date is flagged by audit and never acted on.
- `VERIFICATION.md` lines 352 and 354 cannot be re-measured without `work.json`.
