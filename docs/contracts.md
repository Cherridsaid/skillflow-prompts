# Authoring approved contracts

Three original synthetic examples ship in `examples/contracts` and inside the
wheel. `synthetic-legal` checks a label then requires independent judgment and
human approval. `json-quality` checks an inventory JSON schema. `editorial` proves
extension without core changes. None contains private skill text or real case data.

Validate a contract before an administrator installs it:

```console
uv run skillflow-prompts validate-contract examples/contracts/editorial.json
uv run skillflow-prompts schemas --output schemas --check
```

`schema_version` governs the format; `id@version` is its immutable registry key.
`origin`, `instructions`, resource content/version/SHA-256 and each obligation's
`origin_ref` link the machine contract to the original wording. Installing a
different body at the same version is refused for existing cases. Contract approval
is an administrative filesystem operation, never a writer MCP tool.

Each step has exact inputs (`artifact` or `source:<id>`), required obligations,
acyclic `depends_on`, a correction budget and permitted correction operations.
The default three attempts is configuration, not a measured result. Each failed
attempt consumes budget; a repeated identical failed input is stagnation. A changed
input can invalidate dependent evidence without resetting budgets. JSON schemas
used by verifiers must be self-contained: external references are rejected.

Verifiers are a finite versioned allowlist: `text`, `json`, `source`, `connector`,
`review`. Mechanical validators run internally; connector observations need that
identity; judgments need the independent reviewer. Exit status is never the
verification result. `complete`, `partial` and `absent` describe coverage of the
worded obligation. An absent verifier makes delivery impossible until a newly
approved compatible contract is provisioned; the writer cannot waive it.

Keep judgment obligations in a dedicated step following its mechanical inputs.
Use `review_required` and `human_approval_required` for whole-case policy.
`legal: true` requires both flags. Review verdicts are JUSTE, FAUX and NON_VERIFIE;
all expected judgment instance IDs must appear. A favorable verdict with failed
subjudgments is rejected. A retrieval proof is not a judgment.

`branches` declare allowed operator decisions; a step's `branch` and
`branch_value` control membership. Unknown scope produces a null percentage.
Branches are fixed once decided; changing one needs a new explicitly provisioned
case, preserving the old audit trail. Dependencies cannot point into mutually
exclusive branches. `children` declares fixed named sub-skill slots; required
children exist from the start. Repeated slot requests cannot duplicate the unit.
Cross-skill `dependencies` require matching mandatory profile entries.

Source policies pin exact HTTPS URLs, accepted plain-text/JSON media types, maximum
age and optionally a SHA-256. HTML/extraction workflows and authenticated websites
need a separately reviewed collector; the alpha refuses them. Exact-byte transfer
validation does not establish that the source is substantively correct or applicable.

The packaged `inventory.SKILL.md` and `guarded-note.SKILL.md` are actual synthetic
native skills. `prepare-client-demo` installs exact bytes into the client catalog
and pins them as a contract resource. `artifact_read` reads only the invocation's
current artifact or a declared collected source, never arbitrary native paths.
Installed-file changes stop admission, mutation and delivery until the operator
reprovisions an approved binding.

Private skills may map to this interface locally. Their owners approve the mapping,
resource hashes and verifier coverage; their instructions and documents must not be
copied into this public repository. No particular private skill content is needed.
