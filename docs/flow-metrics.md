# Current progress, historical events and authority

`flow_status` and `coverage_report` compute one SQLite snapshot. The response
includes case/invocation IDs, revision, contract hashes, timestamp, last event ID
and `projection_lag: 0`. Counts are computed by the server; there is no counter
setter in the API. Responses validate against `schemas/flow-status.schema.json`.

The required total counts each active own-work step once, across all invocations.
Parent invocation group headings are not steps. Required child slots are created
at start and cannot disappear through writer input. A correction reuses its step;
it does not increase either the denominator or the number of successes.

`progress_percent = 100 * validated / required_total`. Both integers accompany it.
Zero denominator or unresolved branch scope yields null with `zero_denominator`
or `unresolved_scope`; the CLI displays « non calculable ». This is neither a
time estimate nor a reliability score. No averaging of per-skill percentages occurs.

Current categories are mutually exclusive: NOT_STARTED, IN_PROGRESS, WAITING,
BLOCKED, INVALIDATED, VALIDATED. Their sum equals the current total. The synchronous
mechanical worker does not expose an intermediate IN_PROGRESS transaction; that
category remains zero for it. Obligations have their own satisfied/remaining count,
which is independent of step counts. A multi-obligation step is valid only when
all current proofs pass.

Attempts, correction returns, refusal events and invalidation events are historical
totals; current invalidated or blocked step counts are separate. A repeated event
key returns its stored response. Session resume, process restart and model context
compaction cannot reset budgets. Source freshness is checked on every read/guard;
expired proofs can be currently invalid without a separate timer-generated event.

Changing an artifact/source conservatively invalidates all validated steps in the
case because its review covers the complete snapshot. The demo records a validated
snapshot, then a changed input, a backward movement, a refused delivery, resume,
correction and successful exact export in `demo-report.json`. Those values are
observed only when the demo is actually run. Schema fixtures are not benchmarks.

Review and approval counters show required, received/valid and pending values.
One case-wide review/approval can cover several skills: per-skill values are
explicitly marked shared and must not be summed. Per-skill refusal counts only
include events attributable to that invocation; case-level delivery refusals stay
at case level. Revision-bound approval may become stale after any mutation.

Elapsed wall time is measured. Active operator time, waiting time, model tokens,
provider costs and allocations between skills are null when not measured. Shared
resources are labelled unallocated, avoiding duplicate totals. Refusals are marked
`effect_verified: false`; only an external observation can turn an attempted
forbidden effect into a benchmark PREVENTED outcome.

```console
uv run skillflow-prompts status CASE_ID --token-file OPERATOR_TOKEN --json --output flow-report.json
```

Reports contain private business data and are not automatically published.
Only explicitly selected synthetic benchmark campaigns feed the README generator.
A 100% progress value never substitutes for current delivery guards or human approval.
