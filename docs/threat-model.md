# Threat model and profile acceptance

The adversary is the drafting agent with the actual rights of its account.
The administrator, operator identity, service code, approved registry and trust
anchors form the trusted computing base. An administrator capable of replacing
the installation is outside this boundary.

| Profile | Implemented mechanism | Availability and claim |
|---|---|---|
| Demo | Real state, checks, fixture review/approval, Ed25519 receipts | Executable, same-user, no OS security claim |
| Artifact controlled | Same gate plus external trust, separate credentials, TLS and Windows deployment preflight | Unavailable without independently recorded rights acceptance; not operationally validated here |
| Execution controlled | Artifact gate plus complete evidence for declared native routes | Unavailable without all route tests and a legitimate effect; no universal tool interception claim |

No missing capability silently falls back to demo. A protected key cannot be
replaced by a demo trust anchor: verifiers must supply an externally approved
anchor and, for policy acceptance, an expected profile. Receipts do not contain
self-authorizing public keys. Offline verification establishes historical
authenticity; current revocation status requires the controller or a trusted,
fresh revocation set. A downloaded unsigned revocation list is not current proof.

Protect service code, Python runtime, contracts, configuration, database/WAL,
signing keys, TLS keys, logs, report evidence and output directories from writer
modification. Protect reviewer/operator secrets from writer reads. The operator
must inspect the actual object, not a summary or an UAC prompt. A hook launched
as the writer is still the writer. Client-supplied `role`, `origin` or review
text never creates an authenticated identity.

The HTTP adapter rejects browser Origin headers, arbitrary Host values, redirects,
oversized requests and malformed JSON. Production requires TLS even on loopback
to authenticate the service and prevent token theft through a replacement server.
Do not expose the alpha outside loopback. Tokens are scoped bearer credentials;
their protected storage, rotation and independent identities are deployment work.

Artifacts can contain prompt injection. The reviewer receives explicit data-only
instructions and a read-only Codex workspace; the controller accepts a strict
structured verdict with exact obligation IDs. These measures cannot prove semantic
understanding or protect every model behavior. Original reviewer output is retained.
Mechanical verifiers only check the specified syntax/terms/schema. Partial judgment
coverage stays partial after JUSTE. A human legal decision remains required.

Tests distinguish kernel/adapter effects from Windows permissions and model behavior.
Current untested protected paths include persistent client
terminals, delegated client sessions, ACL denial under a distinct writer account,
production reviewer authentication and real legal interpretation. They are not
counted as prevented attacks. See [reception matrix](reception.md).

Native write denials and absent synthetic canary files were observed under the
recorded client settings. Current-user filesystem probes also demonstrated access
to privileged demo files and destinations. These observations establish neither
complete native interception nor an OS boundary. Protected launchers must also
control interpreter resolution, client settings and inherited process permissions.

Local text/JSON exports are the only implemented side effects. Arbitrary source
plugins, general shell workers, email delivery and document conversion are not
available. Network timeouts, ambiguous outbox files and failed reviews fail closed.
An active same-user adversary can alter demo storage; this is expected and is why
demo is not a protected profile.
