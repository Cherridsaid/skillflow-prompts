# Operations, review, receipts and recovery

Create a fresh, clearly unprotected demonstration authority:

```console
uv run skillflow-prompts init-demo --root .skillflow/manual
uv run skillflow-prompts serve --root .skillflow/manual
```

The service listens only on loopback. A second daemon using the same root is
rejected by an OS file lock. Do not share writer, connector, reviewer and operator
tokens. The demo stores them together for inspection and makes no separation claim.
Protected mode requires TLS and independent protected storage.

Every mutating API envelope has `action`, `arguments`, `revision` and
`idempotency_key`. Read `flow_status` before mutations. Keep a retry's original key
and full envelope when connectivity is ambiguous; changing parameters requires a
new key and current revision. A retry does not reset state or counters.

`review_request` reserves a case-wide bounded review job. The independent reviewer
reads the frozen packet with `review_read`. `skillflow-prompts review CASE REVIEW --engine ENGINE
--token-file REVIEWER_TOKEN --model MODEL --allow-model-call` launches the explicitly
selected engine in a bounded read-only temporary workspace; `review-codex` remains the
Codex-only spelling. Engines: `codex` and `claude-cli`. The engine name is submitted as
the review provider and a contract may list the reviewers it accepts (`reviewers` in the
contract); an engine outside that list is refused, and the refusal names the accepted
ones. The declared model is journalled with the verdict as a reviewer claim. Only that
engine's own account variables reach the child process, never a SkillFlow token. CLI authentication
belongs to that reviewer account. No writer-supplied executable or role is accepted
by the controller. The command is opt-in and was not run against a paid model.
The original JSON response is retained verbatim; a Codex response is exported as
`<artifact>.revue-codex.md`, with controller metadata in the receipt sidecar.

`delivery_request` only prepares a request. `skillflow-prompts approve CASE DELIVERY
--token-file OPERATOR_TOKEN` presents all exact artifact/source bytes as text,
the original review, reservations and destination. The operator must type an
approval phrase containing the full snapshot hash. The credential must be held by
the human operator, not handed to the drafting agent. UAC is not business consent.

The operator's `delivery_commit` atomically checks current evidence/approval and
creates an outbox intent. State becomes RESULT_UNKNOWN before any files are
written. The worker writes exact bytes plus separate receipts, original review
and a manifest. It observes the complete file set before DELIVERED. On failure,
it never overwrites conflicting bytes. `delivery_reconcile` observes the intended
files after restart or inspection. Local exclusive creation supports safe retries;
there is no exactly-once promise for arbitrary external services.

```console
uv run skillflow-prompts verify artifact.receipt.json artifact.txt --trust approved-trust.json --case CASE_ID --profile demo
uv run skillflow-prompts backup --root .skillflow/manual --output verified-backup.db
```

Receipt format `skillflow-ed25519-v1` signs `SkillFlow-Receipt-v1\0 || payload_bytes`
with standard Ed25519. Payload bytes are UTF-8 JSON with sorted keys, no whitespace,
no duplicate keys, no NaN/Infinity and no floating-point claims. Base64 transports
those exact bytes and the signature. The public key is supplied through an approved
external trust map keyed by SHA-256 of the 32-byte Ed25519 public key. Hashes are
identifiers and integrity checks, not independent provenance signatures.

The signed payload binds issuer/key, case, invocation, artifact SHA-256, contracts,
proof IDs, original review digest, approval, time, destination and coverage profile.
Verification cannot establish current authorization using only the historical
receipt. Use authenticated `receipt_verify` to consult controller revocations, or
a separately trusted current list with `--revocations`. Never trust a key embedded
in untrusted receipt data. A file with changed bytes fails verification.

To revoke an issued delivery, the scoped operator calls `receipt_revoke` with its
case/delivery/reason. Revoke a compromised key by adding it to the protected
revocation authority and rotating its private key plus approved trust map while the
service is stopped. Rotation preserves historical trust for archival verification;
it does not automatically reauthorize old documents. Token changes require restart.

SQLite online backup includes frozen artifacts, proofs, events and operations.
It does not include separate signing/TLS keys, tokens, approved contracts or output
directories: back those up through a protected operator procedure. Restore into a
new stopped installation, run `PRAGMA integrity_check`, verify schema version and
recover outbox intentions. Never copy a live database without its WAL or the online
backup API. Unknown schema versions fail closed; only initial migration 0→1 exists.
