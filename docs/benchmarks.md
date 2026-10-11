# Reproducible measurements

Three suites are distinct: deterministic controller, real HTTP integration,
and agent evaluation. The latter records planned NOT_RUN trials by default: no
provider call, model key or charge is inferred from running a benchmark command.
The integration suite exercises real loopback HTTP credentials under one user;
it is not an OS-isolation or live Codex/Claude native-tool campaign.

```console
uv run skillflow-prompts bench run --suite controller --repetitions 3 --output benchmarks/local/new-controller
uv run skillflow-prompts bench run --suite integration --repetitions 3 --output benchmarks/local/new-http
uv run skillflow-prompts bench run --suite agents --repetitions 1 --output benchmarks/local/unrun-agents
uv run skillflow-prompts bench validate --output benchmarks/local/new-controller
uv run skillflow-prompts bench aggregate --output benchmarks/local/new-controller
```

Use new directories: campaigns are never overwritten. The manifest is written
before trials and fixes scenario paths, repetitions, criteria, rights and versions.
Every planned scenario/repetition pair must have exactly one trace, even when it
was not started. Unknown results, infrastructure exceptions and timeouts remain.
skillflow-prompts ships no measurement results: the benchmark code is inherited
from SkillFlow and produces local campaigns only. Reaggregation is deterministic;
rerunning timings is not byte-identical.

Outcome categories are exclusive: PREVENTED, FORBIDDEN_EFFECT, LEGITIMATE_SUCCESS,
FALSE_BLOCK, UNKNOWN, INFRA_ERROR, NOT_RUN. PREVENTED requires the observed absence
of the covered export effect **and** an actual expected refusal. A server error,
unobserved effect or timeout is not a successful prevention. The HTTP identity
scenario checks actual approval rows; local delivery scenarios inspect exported
receipt/artifact files. They do not test native paths outside the gate.

All percentages retain numerator and denominator:

| Measure | Denominator |
|---|---|
| Prevented/bypassed forbidden effects | Forbidden trials started |
| Legitimate successes/false blocks | Legitimate tasks started |
| Unknown/infrastructure outcomes | All trials started |
| Accepted stale review | Stale-review trials started |
| Covered paths | Preregistered paths inventoried |

Zero denominator displays **Not measured yet / Non mesuré**. No outcome disappears
from the total. Measured wall time includes fixture setup, database work and cleanup.
Operation timing separately records the guarded action or export sequence. Median
and nearest-rank p95 include sample size. No target latency becomes an observation.

The environment records tested commit, dirty flag, binary diff hash, an explicit
hash map of source/test/example/schema files, package/contract versions, OS, Python,
machine architecture, rights and coverage profile. The tree hash covers uncommitted
files even before Git tracks them. A dirty run must not be attributed to its commit
alone. README generation changes only bounded marker sections. Its later commit
does not retroactively become the tested source commit.

No paired with/without-controller agent campaign has been executed. Consequently
overhead, comparative model performance, population confidence intervals, tokens,
cost per started/successful task, operator active/wait time and human intervention
rates are **non mesuré**. A future paired campaign must use identical tasks, inputs,
model versions, tools, deadlines and external success criteria; document every
necessary rights/delivery difference and attribute outcomes to the whole configuration.
Sample by task, not correlated retries. Use Wilson intervals for independent task
proportions or a task-cluster bootstrap when multiple attempts share a task. Fixture
reviews are not semantic ground truth.

`benchmarks/agents/evaluate.py` is a separate, opt-in artifact evaluation runner.
It requires explicit client/model, call count and timeout budgets. It measures
model outputs against deterministic synthetic schemas; it does not claim that a
model understands law or that an MCP tool was obligatorily invoked.

Initial local development campaigns may contain infrastructure errors and remain
under ignored `benchmarks/local`. Selected public campaigns identify their source
revision and retain all their trials, including failures. Test counts, code coverage
and zero observed bypasses are never marketed as security percentages.
