# Validation evidence — 2026-08-31

## Automated checks

From the workspace root:

```bash
python3 -m compileall -q pr_crawler tests
python3 -m unittest discover -s tests -v
```

33 tests cover configurable/multiple repositories, all PR states, a synthetic 1,105-PR index, nested REST/GraphQL pagination (including thread comments), time boundaries and invalid input, API caps, duplicates/concurrent changes, partial errors, retries and persisted quota cooldown, restart/idempotence, child refresh independent of parent timestamps, source/probe fingerprints across multiple failures and interruption, invalid/oversized references, media discovery, redirect/IP/credential safety, truncation, wall-clock termination, and archive-auditor corruption detection.

## Live full-repository index

Command:

```bash
VERIFIER_PYTHON report/run.py collect index PrismJS/prism --output crawler-output/live-validation
```

Run ID: `1fc80c9be27b461594066abc52b77100`.

- UTC start/cutoff: `2026-08-31T13:06:00.839480+00:00`.
- UTC completion: `2026-08-31T13:09:43.739299+00:00`.
- Both traversals: 22 pages, 2,185 unique PRs; 44 raw successful responses preserved.
- Bounded ID/updated_at sets matched between traversals; state `complete`, observational rather than atomic consistency.
- 95 open, 2,090 closed; 1,763 merged (subset of closed); 14 draft (overlapping state category).
- No search endpoint or pre-existing dataset PR list was used for enumeration.

Offline time selection:

```bash
VERIFIER_PYTHON report/run.py collect select --output crawler-output/live-validation \
  --run 1fc80c9be27b461594066abc52b77100 --axis created_at \
  --start 2018-01-01 --end 2019-01-01
```

Result: 196 PRs; this command does not access credentials or the network.

## Live rich records and media

```bash
VERIFIER_PYTHON report/run.py collect enrich --output crawler-output/live-validation \
  --source-run 1fc80c9be27b461594066abc52b77100 \
  --pr 'PrismJS/prism#1500' --pr 'PrismJS/prism#1573' \
  --start 2018-01-01 --end 2019-01-01 \
  --download-assets --max-asset-bytes 2097152
```

Run ID: `a713e30a26fd4f2d8d2f8ae12093b36d`, started `2026-08-31T13:10:16.861833+00:00` UTC. This is explicitly two selected detail records, **not** a claim of downloading details for all 2,185 PRs.

| PR | Linked Issues | Reviews | Inline comments | Threads | Downloaded media |
| --- | ---: | ---: | ---: | ---: | ---: |
| [#1500](https://github.com/PrismJS/prism/pull/1500) | 1 | 0 | 0 | 0 | 2 |
| [#1573](https://github.com/PrismJS/prism/pull/1573) | 2 | 5 | 5 | 1 | 7 |

All required sections and requested assets are complete. Issue relationships include GitHub-reported closing relationships; review thread resolution and comment anchors were returned by the actual API. The nine downloaded media have verified lengths and SHA-256 hashes. Sizes in bytes: 25,235; 11,965; 153,816; 9,866; 6,300; 5,908; 3,808; 33,922; 22,430.

### Actual recovery and repeat run

The initial enrichment exited 2: the GraphQL API rejected an unsupported `includeClosed` argument. We inspected live PullRequest field arguments, removed that argument, and resumed **the same run**. This exercised a real partial failure and page reuse. The two original error responses remain in the raw history; they are not silently overwritten.

```bash
VERIFIER_PYTHON report/run.py collect resume --output crawler-output/live-validation \
  --run a713e30a26fd4f2d8d2f8ae12093b36d
```

Recovery exited 0 with both PRs complete. A subsequent identical resume also exited 0 without adding any API response rows: 38 total remained, 36 reusable successes and 2 historical errors. The two normalized PR identities remain unique, and the same nine content-addressed assets were reused. Last repeat completion recorded at `2026-08-31T13:14:11.671337+00:00` UTC. Synthetic tests separately prove process interruption before verification and three-stage failure/recovery cannot mix code versions into a false complete record.

## Independent artifact audit

```bash
python3 tests/verify_archive.py --output crawler-output/live-validation \
  --run 1fc80c9be27b461594066abc52b77100
python3 tests/verify_archive.py --output crawler-output/live-validation \
  --run a713e30a26fd4f2d8d2f8ae12093b36d \
  --require-media --require-review --require-issue
```

Both exit 0 with `audit=passed`. This read-only auditor validates raw response hashes, complete index/selection/detail coverage in SQLite, required sections, response ID provenance, material fingerprint/boundary ordering, downloaded media size/hash/path safety, and exact agreement between SQLite PR records and their exported JSON. It checks the report's completion status and detail count, not every report field or the index/selection JSON export contents. It asserts actual nonempty Issue/review/media evidence when the flags above are used. A unit test proves it rejects corrupted raw bytes.

Generated files reside in `crawler-output/live-validation/` and are not included in source commits. Human-readable report: `exports/a713e30a26fd4f2d8d2f8ae12093b36d/report.md` under that directory. Historical errors are evidence, not remaining failures in the normalized run.

## Independent review

Exactly three read-only reviewers assessed the major implementation milestone: correctness/tests, design/boundaries, security/maintainability. Two fix/re-review rounds resolved initial findings about resume consistency, persistent cooldown, malformed references/media, incomplete manifest coverage, truncated media and total download time. Each independently ran the then-current 31 tests; all three reported no remaining findings. Subsequent validation-only additions include the read-only archive auditor, its corruption regression, and a multi-repository test. A supplemental independent evidence review ran all 33 tests and both live audits successfully; its auditor-description wording correction is reflected above. No source repository code, Docker image, GitHub remote state or credentials were modified.

The official SWE-bench collection-code comparison is documented separately under `research/`; it informs subsequent extraction semantics without changing these archive and verification boundaries.
