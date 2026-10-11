# Contributing

Contributions submitted for inclusion use the project's Apache-2.0 license.
Retain required third-party notices. No separate CLA is imposed.

Install Python 3.12 and uv; run `uv sync --locked`. Work on a branch. Run tests,
ruff checks/formatting, schema freshness, the demo and `uv build` as in README.

For a contract, add an original synthetic fixture and demonstrate it through the
existing core. For an adapter, keep all authority in the controller and test the
actual protocol endpoint. State untested versions and OS paths explicitly.
Privileged testing belongs on an isolated machine, never a personal workstation
executing untrusted PR code.

Exclude private skills, dossiers, secrets, personal paths and tokens. Public
benchmark data must be synthetic. Preserve UNKNOWN/INFRA_ERROR outcomes and raw
traces. Never call test counts a security rate.

Dependencies need a reason, compatible license, immutable lock and at least
72 hours of release age for security/build/runtime components. Change the cutoff
deliberately. Prefer small fixes with a meaningful regression test.

Use the issue/PR templates. Vulnerabilities go through SECURITY.md; do not publish
secrets or full exploit details in issues. No response SLA is promised.
